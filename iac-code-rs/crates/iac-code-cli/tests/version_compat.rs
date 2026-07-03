use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU16, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use iac_code_a2a::push_queue::LocalFileA2APushQueue;
use iac_code_a2a::push_secrets::A2APushSecretKeyring;
use iac_code_core::{sanitize_path, SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME};
use iac_code_protocol::json::JsonValue;

mod common;

use common::{command_fixture, json_string_field, read_http_request};

const TEST_SERVER_ACCEPT_TIMEOUT: Duration = Duration::from_secs(30);
const TEST_SERVER_ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(10);
static NEXT_TEST_PORT_OFFSET: AtomicU16 = AtomicU16::new(0);

fn assert_fixture(name: &str) {
    let expected = command_fixture("cli_basic", name);
    let mut cmd = iac_code_command();
    cmd.args(&expected.argv);
    let output = cmd.output().expect("command runs");

    assert_eq!(
        output.status.code(),
        Some(expected.exit_code),
        "{name} exit code"
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        expected.stdout,
        "{name} stdout"
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        expected.stderr,
        "{name} stderr"
    );
}

fn iac_code_command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_iac-code"));
    command
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8");
    command
}

#[test]
fn unknown_top_level_command_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("unknown-command")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("No such command 'unknown-command'."),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn extra_top_level_argument_after_option_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--prompt", "hello", "extra"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("No such command 'extra'."),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn dash_prefixed_option_values_match_python_typer() {
    let output = iac_code_command()
        .args(["--prompt", "-x", "--output-format", "nope"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid --output-format 'nope'."),
        "{stderr}"
    );
}

#[test]
fn empty_output_format_defaults_to_text_like_python() {
    let output = iac_code_command()
        .args([
            "--prompt",
            "hello",
            "--output-format",
            "",
            "--permission-mode",
            "nope",
        ])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid --permission-mode 'nope'."),
        "{stderr}"
    );
    assert!(!stderr.contains("Invalid --output-format"), "{stderr}");
}

#[test]
fn prompt_stdin_dash_does_not_block_invalid_output_format_like_python() {
    let mut child = iac_code_command()
        .args(["--prompt", "-", "--output-format", "nope"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");

    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    let status = loop {
        if let Some(status) = child.try_wait().expect("command poll succeeds") {
            break status;
        }
        if Instant::now() >= deadline {
            child.kill().expect("hung command should be killed");
            child.wait().expect("killed command should be reaped");
            panic!("command blocked reading stdin before validating --output-format");
        }
        thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
    };

    let mut stdout = String::new();
    child
        .stdout
        .take()
        .expect("stdout is piped")
        .read_to_string(&mut stdout)
        .expect("stdout reads");
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .expect("stderr is piped")
        .read_to_string(&mut stderr)
        .expect("stderr reads");

    assert_eq!(status.code(), Some(1));
    assert_eq!(stdout, "");
    assert!(
        stderr.contains("Invalid --output-format 'nope'."),
        "{stderr}"
    );
}

#[test]
fn long_option_equals_values_match_python_typer() {
    let output = iac_code_command()
        .args(["--prompt=hello", "--output-format=nope"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid --output-format 'nope'."),
        "{stderr}"
    );
}

#[test]
fn short_option_attached_values_match_python_typer() {
    let output = iac_code_command()
        .args(["-phello", "--output-format=nope"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid --output-format 'nope'."),
        "{stderr}"
    );
}

#[test]
fn invalid_max_turns_value_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--prompt", "hello", "--max-turns", "nope"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid value for '--max-turns': 'nope' is not a valid integer."),
        "{stderr}"
    );
}

#[test]
fn negative_max_turns_value_is_accepted_like_python_typer() {
    let output = iac_code_command()
        .args([
            "--prompt",
            "hello",
            "--max-turns",
            "-1",
            "--output-format",
            "nope",
        ])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid --output-format 'nope'."),
        "{stderr}"
    );
}

#[test]
fn unknown_a2a_client_subcommand_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a-client", "unknown-command"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("No such command 'unknown-command'."),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn removed_top_level_a2a_client_commands_are_rejected_like_python_typer() {
    for command in ["a2a-call", "a2a-route-preview"] {
        let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
            .args([command, "--help"])
            .output()
            .expect("command runs");
        let stderr = String::from_utf8_lossy(&output.stderr);

        assert_eq!(output.status.code(), Some(2), "{command}: {stderr}");
        assert_eq!(String::from_utf8_lossy(&output.stdout), "");
        assert!(
            stderr.contains(&format!("No such command '{command}'.")),
            "{command}: {stderr}"
        );
    }
}

#[test]
fn unknown_top_level_option_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--not-a-real-option")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("No such option: --not-a-real-option"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn missing_top_level_option_value_errors_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--model")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("Option '--model' requires an argument."),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn update_command_help_prints_subcommand_usage() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["update", "--help"])
        .output()
        .expect("command runs");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0));
    assert!(
        stdout.contains("Usage: iac-code update [OPTIONS]"),
        "{stdout}"
    );
    assert!(stdout.contains("--check"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn update_command_help_uses_chinese_locale_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["update", "--help"])
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .output()
        .expect("command runs");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0));
    assert!(stdout.contains("仅检查更新，不安装。"), "{stdout}");
    assert!(
        !stdout.contains("Check for updates without installing."),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn update_command_reports_local_build_boundary_without_starting_repl() {
    for args in [["update"].as_slice(), ["update", "--check"].as_slice()] {
        let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
            .args(args)
            .output()
            .expect("command runs");

        let stderr = String::from_utf8_lossy(&output.stderr);
        assert_eq!(output.status.code(), Some(1), "{args:?}");
        assert_eq!(String::from_utf8_lossy(&output.stdout), "", "{args:?}");
        assert!(
            stderr.contains("self-update is not available in Rust local builds"),
            "{args:?}: {stderr}"
        );
    }
}

fn json_string_path<'a>(value: &'a JsonValue, path: &[&str]) -> Option<&'a str> {
    let mut current = value;
    for key in path {
        let JsonValue::Object(object) = current else {
            return None;
        };
        current = object.get(*key)?;
    }
    match current {
        JsonValue::String(value) => Some(value),
        _ => None,
    }
}

#[test]
fn version_long_matches_python_fixture() {
    assert_fixture("version_long");
}

#[test]
fn version_short_lower_matches_python_fixture() {
    assert_fixture("version_short_lower");
}

#[test]
fn version_short_upper_matches_python_fixture() {
    assert_fixture("version_short_upper");
}

#[test]
fn help_short_and_long_print_usage_and_exit_zero() {
    for flag in ["--help", "-h"] {
        let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
            .arg(flag)
            .output()
            .expect("command runs");
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert_eq!(output.status.code(), Some(0), "{flag} exit code");
        assert_eq!(String::from_utf8_lossy(&output.stderr), "", "{flag} stderr");
        assert!(stdout.contains("Usage: iac-code"), "{stdout}");
        assert!(stdout.contains("--prompt"), "{stdout}");
        assert!(stdout.contains("--output-format"), "{stdout}");
        assert!(stdout.contains("--permission-mode"), "{stdout}");
        assert!(stdout.contains("--install-completion"), "{stdout}");
        assert!(stdout.contains("--show-completion"), "{stdout}");
        assert!(stdout.contains("a2a-client"), "{stdout}");
    }
}

#[test]
fn top_level_help_uses_chinese_locale_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--help")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .output()
        .expect("command runs");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0));
    assert!(stdout.contains("AI 驱动的基础设施编排工具"), "{stdout}");
    assert!(
        stdout.contains("非交互模式：运行单个提示并退出"),
        "{stdout}"
    );
    assert!(stdout.contains("将 iac-code 更新到最新版本。"), "{stdout}");
    assert!(
        !stdout.contains("AI-powered infrastructure orchestration tool"),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn show_completion_outputs_side_by_side_zsh_script() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--show-completion")
        .env("SHELL", "/bin/zsh")
        .output()
        .expect("command runs");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0));
    assert!(stdout.contains("#compdef iac-code"), "{stdout}");
    assert!(
        stdout.contains("_IAC_CODE_COMPLETE=complete_zsh"),
        "{stdout}"
    );
    assert!(
        stdout.contains("compdef _iac_code_completion iac-code"),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn completion_runtime_returns_top_level_zsh_matches() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("_IAC_CODE_COMPLETE", "complete_zsh")
        .env("_TYPER_COMPLETE_ARGS", "iac-code --")
        .output()
        .expect("command runs");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0));
    assert!(
        stdout.contains(r#""--prompt":"Non-interactive mode"#),
        "{stdout}"
    );
    assert!(
        stdout.contains(r#""--show-completion":"Show completion"#),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");

    let command_output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("_IAC_CODE_COMPLETE", "complete_zsh")
        .env("_TYPER_COMPLETE_ARGS", "iac-code a")
        .output()
        .expect("command runs");
    let command_stdout = String::from_utf8_lossy(&command_output.stdout);
    assert_eq!(command_output.status.code(), Some(0));
    assert!(command_stdout.contains(r#""acp":"Run iac-code as an ACP server.""#));
    assert!(command_stdout.contains(r#""a2a-client":"Use iac-code as an A2A client.""#));
    assert!(!command_stdout.contains(r#""update":"#), "{command_stdout}");
}

#[test]
fn install_completion_reports_local_build_boundary_without_writing_shell_files() {
    let home_dir = temp_dir("install-completion-home");
    fs::create_dir_all(&home_dir).expect("home dir should be created");
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--install-completion")
        .env("HOME", &home_dir)
        .env("SHELL", "/bin/zsh")
        .output()
        .expect("command runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("completion installation is not available in Rust local builds"),
        "{stderr}"
    );
    assert!(!home_dir.join(".zshrc").exists());
    fs::remove_dir_all(&home_dir).ok();
}

#[test]
fn a2a_command_help_prints_protocol_options_and_exit_zero() {
    let cases = [
        (
            vec!["acp", "--help"],
            vec!["Usage: iac-code acp", "--transport", "--host", "--port"],
        ),
        (
            vec!["a2a", "--help"],
            vec!["Usage: iac-code a2a", "--transport", "--host", "--port"],
        ),
        (
            vec!["a2a", "--transport", "http", "--help"],
            vec!["Usage: iac-code a2a", "--transport", "--host", "--port"],
        ),
        (
            vec!["a2a-client", "--help"],
            vec![
                "Usage: iac-code a2a-client",
                "call",
                "discover",
                "task-get",
                "route-preview",
            ],
        ),
        (
            vec![
                "a2a-client",
                "--config",
                "/tmp/iac-code-a2a-client.yml",
                "task-get",
                "--help",
            ],
            vec![
                "Usage: iac-code a2a-client task-get",
                "--task-id",
                "--history-length",
            ],
        ),
        (
            vec!["a2a-client", "call", "--help"],
            vec![
                "Usage: iac-code a2a-client call",
                "--url",
                "--prompt",
                "--stream",
            ],
        ),
    ];

    for (argv, expected_fragments) in cases {
        let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
            .args(argv)
            .output()
            .expect("command runs");
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert_eq!(output.status.code(), Some(0), "{stdout}");
        assert_eq!(String::from_utf8_lossy(&output.stderr), "", "{stdout}");
        for fragment in expected_fragments {
            assert!(stdout.contains(fragment), "{stdout}");
        }
        if stdout.contains("Usage: iac-code a2a-client") {
            assert!(!stdout.contains("a2a-route-preview"), "{stdout}");
        }
    }
}

#[test]
fn a2a_client_task_and_route_help_matches_python_options() {
    let cases = [
        (
            vec!["a2a-client", "task-get", "--help"],
            vec![
                "--history-length",
                "Maximum task history items to return",
                "--basic-username",
                "--basic-password",
                "--api-key",
                "--api-key-header",
            ],
            vec!["--timeout"],
        ),
        (
            vec!["a2a-client", "task-list", "--help"],
            vec![
                "--status",
                "--page-size",
                "--page-token",
                "--include-artifacts",
                "--output",
                "Output format: table or json",
                "--basic-username",
                "--basic-password",
                "--api-key",
                "--api-key-header",
            ],
            vec!["--state", "--limit", "--timeout"],
        ),
        (
            vec!["a2a-client", "task-cancel", "--help"],
            vec![
                "--basic-username",
                "--basic-password",
                "--api-key",
                "--api-key-header",
            ],
            vec!["--timeout"],
        ),
        (
            vec!["a2a-client", "route-preview", "--help"],
            vec![
                "--name",
                "--skill",
                "--route-state-dir",
                "--persistence-dir",
                "--save-routes",
            ],
            vec!["--route-name"],
        ),
    ];

    for (argv, expected_fragments, absent_fragments) in cases {
        let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
            .args(argv)
            .output()
            .expect("command runs");
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert_eq!(output.status.code(), Some(0), "{stdout}");
        assert_eq!(String::from_utf8_lossy(&output.stderr), "", "{stdout}");
        for fragment in expected_fragments {
            assert!(stdout.contains(fragment), "{stdout}");
        }
        for fragment in absent_fragments {
            assert!(!stdout.contains(fragment), "{stdout}");
        }
    }
}

#[test]
fn a2a_server_http_serves_health_and_agent_card() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let health = wait_for_http_get(port, "/health");
    let card = http_get(port, "/.well-known/agent-card.json");
    stop_child(&mut child);

    assert!(health.starts_with("HTTP/1.1 200 OK\r\n"), "{health}");
    assert!(health.contains("{\"status\":\"healthy\"}"), "{health}");
    assert!(card.starts_with("HTTP/1.1 200 OK\r\n"), "{card}");
    assert!(card.contains("\"name\":\"iac-code\""), "{card}");
    assert!(
        card.contains(&format!("\"url\":\"http://127.0.0.1:{port}/\"")),
        "{card}"
    );
    assert!(card.contains("\"enabledTypes\":[\"tool_trace\"]"), "{card}");
}

#[test]
fn a2a_server_http_advertises_configured_thinking_exposure() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--thinking-exposure",
            "raw-thinking",
            "--thinking-exposure",
            "tool_trace",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_http_get(port, "/health");
    let card = http_get(port, "/.well-known/agent-card.json");
    stop_child(&mut child);

    assert!(
        card.contains("\"uri\":\"urn:iac-code:a2a:thinking-exposure:v1\""),
        "{card}"
    );
    assert!(
        card.contains("\"enabledTypes\":[\"raw_thinking\",\"tool_trace\"]"),
        "{card}"
    );
}

#[test]
fn a2a_server_http_config_accepts_yaml_list_thinking_exposure_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-thinking-list-config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!(
            "host: 127.0.0.1\nport: {port}\ntransport: http\nthinking-exposure:\n  - raw-thinking\n  - tool-trace\n"
        ),
    )
    .expect("a2a config should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config"])
        .arg(&config_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_http_get(port, "/health");
    let card = http_get(port, "/.well-known/agent-card.json");
    stop_child(&mut child);
    fs::remove_dir_all(&config_dir).ok();

    assert!(
        card.contains("\"enabledTypes\":[\"raw_thinking\",\"tool_trace\"]"),
        "{card}"
    );
}

#[test]
fn a2a_server_http_config_api_key_advertises_and_enforces_auth_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-http-api-key-config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!(
            "host: 127.0.0.1\nport: {port}\ntransport: http\napi_key: server-secret\napi_key_header: X-IAC-Code-Key\n"
        ),
    )
    .expect("a2a config should be written");
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config"])
        .arg(&config_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let health = wait_for_http_get(port, "/health");
    let card = http_get_with_headers(
        port,
        "/.well-known/agent-card.json",
        &[("X-IAC-Code-Key", "server-secret")],
    );
    let unauthorized = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"card-denied","method":"agent/getAuthenticatedExtendedCard","params":{}}"#,
    );
    let authorized = http_post_with_headers(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"card-ok","method":"agent/getAuthenticatedExtendedCard","params":{}}"#,
        &[("X-IAC-Code-Key", "server-secret")],
    );
    stop_child(&mut child);
    fs::remove_dir_all(&config_dir).ok();

    assert!(health.starts_with("HTTP/1.1 401 "), "{health}");
    assert!(health.contains("{\"error\":\"Unauthorized\"}"), "{health}");
    assert!(card.starts_with("HTTP/1.1 200 OK\r\n"), "{card}");
    assert!(card.contains("\"apiKeyAuth\""), "{card}");
    assert!(card.contains("\"name\":\"X-IAC-Code-Key\""), "{card}");
    assert!(unauthorized.starts_with("HTTP/1.1 401 "), "{unauthorized}");
    assert!(
        unauthorized.contains("{\"error\":\"Unauthorized\"}"),
        "{unauthorized}"
    );
    assert!(
        authorized.starts_with("HTTP/1.1 200 OK\r\n"),
        "{authorized}"
    );
    assert!(authorized.contains("\"id\":\"card-ok\""), "{authorized}");
    assert!(authorized.contains("\"apiKeyAuth\""), "{authorized}");
}

#[test]
fn acp_server_http_bridges_jsonrpc_over_sse() {
    let port = free_tcp_port();
    let config_dir = temp_dir("acp-http-config");
    let workspace_dir = temp_dir("acp-http-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "acp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IACCODE_ACP_HTTP_TOKEN")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let health = wait_for_http_get(port, "/health");
    let init_response = http_post(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"init-http","method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"rust-http-test","version":"1.0"}}}"#,
    );
    let conn_id = http_response_header(&init_response, "Acp-Connection-Id")
        .expect("initialize response includes connection id");
    let missing_connection = http_post(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"new-missing","method":"session/new","params":{"cwd":"."}}"#,
    );
    let new_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"new-http","method":"session/new","params":{{"cwd":"{}"}}}}"#,
            workspace_dir.display()
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    let sse_response = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"new-http\"");
    stop_child(&mut child);
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert!(health.starts_with("HTTP/1.1 200 OK\r\n"), "{health}");
    assert!(health.contains("{\"status\":\"healthy\"}"), "{health}");
    assert!(
        init_response.starts_with("HTTP/1.1 200 OK\r\n"),
        "{init_response}"
    );
    assert!(
        init_response.contains("\"id\":\"init-http\""),
        "{init_response}"
    );
    assert!(
        init_response.contains("\"protocolVersion\":1"),
        "{init_response}"
    );
    assert!(
        missing_connection.starts_with("HTTP/1.1 400 "),
        "{missing_connection}"
    );
    assert!(
        missing_connection.contains("Connection not found"),
        "{missing_connection}"
    );
    assert!(
        new_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{new_response}"
    );
    assert!(
        sse_response.contains("Content-Type: text/event-stream"),
        "{sse_response}"
    );
    assert!(sse_response.contains("event: message"), "{sse_response}");
    assert!(
        sse_response.contains("\"id\":\"new-http\""),
        "{sse_response}"
    );
    assert!(sse_response.contains("\"sessionId\":"), "{sse_response}");
    assert!(sse_response.contains("\"models\":"), "{sse_response}");
}

#[test]
fn acp_server_http_honors_client_selected_permission_response() {
    let port = free_tcp_port();
    let config_dir = temp_dir("acp-http-permission-allow");
    let workspace_dir = temp_dir("acp-http-permission-allow-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let responses = [
            r#"{
                "id": "chatcmpl_http_permission_allow_tool",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_http_write_allow",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"http-permission-allowed.txt\",\"content\":\"allowed by http client\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
            r#"{
                "id": "chatcmpl_http_permission_allow_done",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "http permission flow done"}
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}
            }"#,
        ];
        let mut requests = Vec::new();
        for body in responses {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("provider request should have read timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write provider response");
            requests.push(request);
        }
        requests
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "acp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IACCODE_ACP_HTTP_TOKEN")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_http_get(port, "/health");
    let init_response = http_post(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"init-http-allow","method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"rust-http-test","version":"1.0"}}}"#,
    );
    let conn_id = http_response_header(&init_response, "Acp-Connection-Id")
        .expect("initialize response includes connection id");
    let new_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"new-http-allow","method":"session/new","params":{{"cwd":"{}"}}}}"#,
            workspace_dir.display()
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        new_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{new_response}"
    );
    let new_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"new-http-allow\"");
    let session_id = json_string_field(&new_sse, "sessionId");

    let prompt_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"prompt-http-allow","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"write an allowed file over http"}}]}}}}"#
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        prompt_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{prompt_response}"
    );
    let permission_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"permission-0\"");
    assert!(
        permission_sse.contains("\"method\":\"session/request_permission\""),
        "{permission_sse}"
    );
    assert!(
        permission_sse.contains("\"toolCallId\":\"permission/call_http_write_allow\""),
        "{permission_sse}"
    );
    let permission_response = http_post_with_headers(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"permission-0","result":{"outcome":{"outcome":"selected","optionId":"allow_once"}}}"#,
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        permission_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{permission_response}"
    );
    let final_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"prompt-http-allow\"");
    stop_child(&mut child);
    let provider_requests = server.join().expect("provider server finishes");

    assert!(
        final_sse.contains("Successfully wrote 1 lines"),
        "{final_sse}"
    );
    assert!(
        final_sse.contains("http permission flow done"),
        "{final_sse}"
    );
    assert!(
        final_sse.contains("\"stopReason\":\"end_turn\""),
        "{final_sse}"
    );
    assert!(
        provider_requests[1].contains("Successfully wrote 1 lines"),
        "{}",
        provider_requests[1]
    );
    assert_eq!(
        fs::read_to_string(workspace_dir.join("http-permission-allowed.txt"))
            .expect("allowed file should be written"),
        "allowed by http client"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn acp_server_http_cancels_prompt_while_permission_request_is_pending() {
    let port = free_tcp_port();
    let config_dir = temp_dir("acp-http-permission-cancel");
    let workspace_dir = temp_dir("acp-http-permission-cancel-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let body = r#"{
            "id": "chatcmpl_http_permission_cancel_tool",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": null,
                    "tool_calls": [{
                        "id": "call_http_write_cancel",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "{\"path\":\"http-permission-cancelled.txt\",\"content\":\"should not be written\"}"
                        }
                    }]
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
        }"#;
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("provider request should have read timeout");
        let request = read_http_request(&mut stream);
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write provider response");
        request
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "acp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IACCODE_ACP_HTTP_TOKEN")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_http_get(port, "/health");
    let init_response = http_post(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"init-http-cancel","method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"rust-http-test","version":"1.0"}}}"#,
    );
    let conn_id = http_response_header(&init_response, "Acp-Connection-Id")
        .expect("initialize response includes connection id");
    let new_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"new-http-cancel","method":"session/new","params":{{"cwd":"{}"}}}}"#,
            workspace_dir.display()
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        new_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{new_response}"
    );
    let new_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"new-http-cancel\"");
    let session_id = json_string_field(&new_sse, "sessionId");

    let prompt_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"prompt-http-cancel","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"write then cancel over http"}}]}}}}"#
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        prompt_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{prompt_response}"
    );
    let permission_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"permission-0\"");
    assert!(
        permission_sse.contains("\"toolCallId\":\"permission/call_http_write_cancel\""),
        "{permission_sse}"
    );

    let cancel_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"cancel-http-pending","method":"session/cancel","params":{{"sessionId":"{session_id}"}}}}"#
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        cancel_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{cancel_response}"
    );
    let cancel_sse =
        wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"cancel-http-pending\"");
    let final_sse = if cancel_sse.contains("\"id\":\"prompt-http-cancel\"") {
        cancel_sse.clone()
    } else {
        wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"prompt-http-cancel\"")
    };
    let cancel_flow_sse = format!("{cancel_sse}{final_sse}");
    stop_child(&mut child);
    let provider_request = server.join().expect("provider server finishes");

    assert!(cancel_sse.contains("\"result\":null"), "{cancel_sse}");
    assert!(
        cancel_flow_sse.contains("Tool execution cancelled."),
        "{cancel_flow_sse}"
    );
    assert!(
        final_sse.contains("\"stopReason\":\"cancelled\""),
        "{final_sse}"
    );
    assert!(
        provider_request.contains("\"content\":\"write then cancel over http\""),
        "{provider_request}"
    );
    assert!(
        !workspace_dir.join("http-permission-cancelled.txt").exists(),
        "cancelled write_file tool should not create the file"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn acp_server_http_sse_waits_for_future_prompt_events() {
    let port = free_tcp_port();
    let config_dir = temp_dir("acp-http-sse-wait");
    let workspace_dir = temp_dir("acp-http-sse-wait-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let body = r#"{
            "id": "chatcmpl_http_sse_wait_tool",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": null,
                    "tool_calls": [{
                        "id": "call_http_sse_wait",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "{\"path\":\"http-sse-wait.txt\",\"content\":\"pending\"}"
                        }
                    }]
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
        }"#;
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("provider request should have read timeout");
        let request = read_http_request(&mut stream);
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write provider response");
        request
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "acp",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IACCODE_ACP_HTTP_TOKEN")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_http_get(port, "/health");
    let init_response = http_post(
        port,
        "/acp",
        r#"{"jsonrpc":"2.0","id":"init-http-sse-wait","method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"rust-http-test","version":"1.0"}}}"#,
    );
    let conn_id = http_response_header(&init_response, "Acp-Connection-Id")
        .expect("initialize response includes connection id");
    let new_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"new-http-sse-wait","method":"session/new","params":{{"cwd":"{}"}}}}"#,
            workspace_dir.display()
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        new_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{new_response}"
    );
    let new_sse = wait_for_acp_http_sse_contains(port, &conn_id, "\"id\":\"new-http-sse-wait\"");
    let session_id = json_string_field(&new_sse, "sessionId");

    let pending_sse = {
        let conn_id = conn_id.clone();
        thread::spawn(move || {
            http_get_with_headers(port, "/acp", &[("Acp-Connection-Id", &conn_id)])
        })
    };
    thread::sleep(Duration::from_millis(50));
    let prompt_response = http_post_with_headers(
        port,
        "/acp",
        &format!(
            r#"{{"jsonrpc":"2.0","id":"prompt-http-sse-wait","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"trigger permission after sse wait"}}]}}}}"#
        ),
        &[("Acp-Connection-Id", &conn_id)],
    );
    assert!(
        prompt_response.starts_with("HTTP/1.1 202 Accepted\r\n"),
        "{prompt_response}"
    );
    let sse_response = pending_sse.join().expect("pending SSE request completes");
    stop_child(&mut child);
    let provider_request = server.join().expect("provider server finishes");

    assert!(
        sse_response.contains("\"id\":\"permission-0\""),
        "{sse_response}"
    );
    assert!(
        sse_response.contains("\"toolCallId\":\"permission/call_http_sse_wait\""),
        "{sse_response}"
    );
    assert!(
        provider_request.contains("\"content\":\"trigger permission after sse wait\""),
        "{provider_request}"
    );
    assert!(
        !workspace_dir.join("http-sse-wait.txt").exists(),
        "permission remains pending so write_file should not run"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn a2a_server_http_handles_extended_card_jsonrpc() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "extended-card",
            "--url",
            &format!("http://127.0.0.1:{port}/"),
        ])
        .output()
        .expect("client runs");
    stop_child(&mut child);

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"id\":"), "{stdout}");
    assert!(stdout.contains("\"jsonrpc\": \"2.0\""), "{stdout}");
    assert!(stdout.contains("\"name\": \"iac-code\""), "{stdout}");
    assert!(stdout.contains("\"extendedAgentCard\": true"), "{stdout}");
}

#[test]
fn a2a_server_http_handles_task_jsonrpc_lifecycle() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-e2e-1","taskId":"task-e2e-1","contextId":"ctx-e2e-1","role":"ROLE_USER","parts":[{{"text":"hello from e2e"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let list_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"list-1","method":"ListTasks","params":{"contextId":"ctx-e2e-1","status":"TASK_STATE_WORKING"}}"#,
    );
    let get_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"get-1","method":"GetTask","params":{"id":"task-e2e-1"}}"#,
    );
    let cancel_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"cancel-1","method":"CancelTask","params":{"id":"task-e2e-1"}}"#,
    );
    let get_after_cancel_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"get-2","method":"GetTask","params":{"id":"task-e2e-1"}}"#,
    );
    stop_child(&mut child);

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{send_response}"
    );

    assert!(
        list_response.contains("\"id\":\"task-e2e-1\""),
        "{list_response}"
    );
    assert!(
        list_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{list_response}"
    );
    assert!(
        list_response.contains("\"contextId\":\"ctx-e2e-1\""),
        "{list_response}"
    );

    assert!(
        get_response.contains("\"id\":\"task-e2e-1\""),
        "{get_response}"
    );
    assert!(
        get_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{get_response}"
    );

    assert!(
        cancel_response.contains("\"state\":\"TASK_STATE_CANCELED\""),
        "{cancel_response}"
    );

    assert!(
        get_after_cancel_response.contains("\"state\":\"TASK_STATE_CANCELED\""),
        "{get_after_cancel_response}"
    );
}

#[test]
fn a2a_server_http_handles_rest_message_send_like_python() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let body = format!(
        r#"{{"message":{{"messageId":"msg-rest-1","role":"ROLE_USER","parts":[{{"text":"hello rest"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let response = http_post(port, "/message:send", &body);
    stop_child(&mut child);

    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"), "{response}");
    assert!(response.contains("\"task\":{"), "{response}");
    assert!(response.contains("\"id\":\"task-1\""), "{response}");
    assert!(
        response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{response}"
    );
    assert!(
        response.contains("\"text\":\"fixture response: hello rest\""),
        "{response}"
    );
    assert!(!response.contains("\"jsonrpc\""), "{response}");
}

#[test]
fn a2a_server_http_handles_rest_extended_agent_card_like_python() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let response = http_get(port, "/extendedAgentCard");
    stop_child(&mut child);

    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"), "{response}");
    assert!(response.contains("\"name\":\"iac-code\""), "{response}");
    assert!(
        response.contains("\"extendedAgentCard\":true"),
        "{response}"
    );
    assert!(!response.contains("\"jsonrpc\""), "{response}");
}

#[test]
fn a2a_server_http_handles_v03_jsonrpc_task_aliases() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-v03","method":"message/send","params":{{"message":{{"messageId":"msg-v03-1","taskId":"task-v03-1","contextId":"ctx-v03-1","role":"ROLE_USER","parts":[{{"text":"hello from v03"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let get_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"get-v03","method":"tasks/get","params":{"id":"task-v03-1"}}"#,
    );
    let cancel_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"cancel-v03","method":"tasks/cancel","params":{"id":"task-v03-1"}}"#,
    );
    stop_child(&mut child);

    assert!(
        send_response.contains("\"id\":\"send-v03\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{send_response}"
    );
    assert!(
        get_response.contains("\"id\":\"get-v03\""),
        "{get_response}"
    );
    assert!(
        get_response.contains("\"id\":\"task-v03-1\""),
        "{get_response}"
    );
    assert!(
        cancel_response.contains("\"id\":\"cancel-v03\""),
        "{cancel_response}"
    );
    assert!(
        cancel_response.contains("\"state\":\"TASK_STATE_CANCELED\""),
        "{cancel_response}"
    );
}

#[test]
fn a2a_server_http_handles_send_streaming_message_as_sse() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let body = format!(
        r#"{{"jsonrpc":"2.0","id":"stream-send","method":"SendStreamingMessage","params":{{"message":{{"messageId":"msg-stream-send-1","taskId":"task-stream-send-1","contextId":"ctx-stream-send-1","role":"ROLE_USER","parts":[{{"text":"hello stream server"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let response = http_post(port, "/", &body);
    stop_child(&mut child);

    assert!(
        response.contains("Content-Type: text/event-stream"),
        "{response}"
    );
    assert!(response.contains("data: "), "{response}");
    assert!(response.contains("\"id\":\"stream-send\""), "{response}");
    assert!(
        response.contains("\"id\":\"task-stream-send-1\""),
        "{response}"
    );
    assert!(
        response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{response}"
    );
}

#[test]
fn a2a_server_http_handles_task_subscribe_sse() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-subscribe-1","taskId":"task-subscribe-1","contextId":"ctx-subscribe-1","role":"ROLE_USER","parts":[{{"text":"hello from subscribe"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let subscribe_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"subscribe-1","method":"SubscribeToTask","params":{"id":"task-subscribe-1"}}"#,
    );
    stop_child(&mut child);

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{send_response}"
    );
    assert!(
        subscribe_response.contains("Content-Type: text/event-stream"),
        "{subscribe_response}"
    );
    assert!(
        subscribe_response.contains("data: "),
        "{subscribe_response}"
    );
    assert!(
        subscribe_response.contains("\"id\":\"task-subscribe-1\""),
        "{subscribe_response}"
    );
    assert!(
        subscribe_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{subscribe_response}"
    );
    assert!(
        subscribe_response.contains("\"contextId\":\"ctx-subscribe-1\""),
        "{subscribe_response}"
    );
}

#[test]
fn a2a_server_http_executes_fake_agent_for_send_message() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-agent-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-agent-1","taskId":"task-agent-1","contextId":"ctx-agent-1","role":"ROLE_USER","parts":[{{"text":"hello a2a agent"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let get_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"get-agent-1","method":"GetTask","params":{"id":"task-agent-1"}}"#,
    );
    stop_child(&mut child);

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("\"text\":\"fixture response: hello a2a agent\""),
        "{send_response}"
    );
    assert!(
        get_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{get_response}"
    );
    assert!(
        get_response.contains("\"text\":\"fixture response: hello a2a agent\""),
        "{get_response}"
    );
}

#[test]
fn a2a_server_http_denies_tool_permissions_by_default_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-http-permission-default-deny");
    let workspace_dir = temp_dir("a2a-http-permission-default-deny-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let responses = [
            r#"{
                "id": "chatcmpl_a2a_default_permission_tool",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_a2a_default_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"a2a-default-denied.txt\",\"content\":\"should not be written\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
            r#"{
                "id": "chatcmpl_a2a_default_permission_done",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "a2a default permission flow done"}
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}
            }"#,
        ];
        let mut requests = Vec::new();
        for body in responses {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("provider request should have read timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write provider response");
            requests.push(request);
        }
        requests
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-a2a-deny-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-a2a-deny-1","taskId":"task-a2a-deny-1","contextId":"ctx-a2a-deny-1","role":"ROLE_USER","parts":[{{"text":"write a denied a2a file"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}}}"#,
        workspace_dir.display()
    );
    let send_response = http_post(port, "/", &send_body);
    stop_child(&mut child);
    let provider_requests = server.join().expect("provider server finishes");

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("a2a default permission flow done"),
        "{send_response}"
    );
    assert!(
        provider_requests[1].contains("Permission denied."),
        "{}",
        provider_requests[1]
    );
    assert!(
        !workspace_dir.join("a2a-default-denied.txt").exists(),
        "default A2A permission policy should not write files without explicit approval"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn a2a_server_http_auto_approves_tool_permissions_when_configured_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-http-permission-config-allow");
    let workspace_dir = temp_dir("a2a-http-permission-config-allow-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!("host: 127.0.0.1\nport: {port}\ntransport: http\nauto-approve-permissions: true\n"),
    )
    .expect("a2a config should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let responses = [
            r#"{
                "id": "chatcmpl_a2a_config_permission_tool",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_a2a_config_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"a2a-config-allowed.txt\",\"content\":\"allowed by a2a config\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
            r#"{
                "id": "chatcmpl_a2a_config_permission_done",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "a2a configured permission flow done"}
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}
            }"#,
        ];
        let mut requests = Vec::new();
        for body in responses {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("provider request should have read timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write provider response");
            requests.push(request);
        }
        requests
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config"])
        .arg(&config_path)
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-a2a-allow-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-a2a-allow-1","taskId":"task-a2a-allow-1","contextId":"ctx-a2a-allow-1","role":"ROLE_USER","parts":[{{"text":"write an allowed a2a file"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}}}"#,
        workspace_dir.display()
    );
    let send_response = http_post(port, "/", &send_body);
    stop_child(&mut child);
    let provider_requests = server.join().expect("provider server finishes");

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("a2a configured permission flow done"),
        "{send_response}"
    );
    assert!(
        provider_requests[1].contains("Successfully wrote 1 lines"),
        "{}",
        provider_requests[1]
    );
    assert_eq!(
        fs::read_to_string(workspace_dir.join("a2a-config-allowed.txt"))
            .expect("allowed A2A file should be written"),
        "allowed by a2a config"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn a2a_server_http_handles_push_config_jsonrpc_lifecycle() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-push-server");
    fs::remove_dir_all(&config_dir).ok();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args([
            "a2a",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--push-notifications",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-1","method":"SendMessage","params":{{"message":{{"messageId":"msg-push-1","taskId":"task-push-1","contextId":"ctx-push-1","role":"ROLE_USER","parts":[{{"text":"hello from push"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let create_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"create-1","method":"CreateTaskPushNotificationConfig","params":{"taskId":"task-push-1","id":"cfg-1","url":"https://callback.example/a2a","token":"notify-token","authentication":{"scheme":"bearer","credentials":"callback-secret"}}}"#,
    );
    let list_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"list-1","method":"ListTaskPushNotificationConfigs","params":{"taskId":"task-push-1"}}"#,
    );
    let get_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"get-1","method":"GetTaskPushNotificationConfig","params":{"taskId":"task-push-1","id":"cfg-1"}}"#,
    );
    let rejected_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"reject-1","method":"CreateTaskPushNotificationConfig","params":{"taskId":"task-push-1","id":"cfg-local","url":"http://127.0.0.1:9999/a2a"}}"#,
    );
    let delete_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"delete-1","method":"DeleteTaskPushNotificationConfig","params":{"taskId":"task-push-1","id":"cfg-1"}}"#,
    );
    stop_child(&mut child);
    fs::remove_dir_all(&config_dir).ok();

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{send_response}"
    );

    assert!(
        create_response.contains("\"id\":\"cfg-1\""),
        "{create_response}"
    );
    assert!(
        create_response.contains("\"taskId\":\"task-push-1\""),
        "{create_response}"
    );
    assert!(
        create_response.contains("\"scheme\":\"bearer\""),
        "{create_response}"
    );
    assert!(
        !create_response.contains("notify-token"),
        "{create_response}"
    );
    assert!(
        !create_response.contains("callback-secret"),
        "{create_response}"
    );

    assert!(list_response.contains("\"configs\""), "{list_response}");
    assert!(
        list_response.contains("\"id\":\"cfg-1\""),
        "{list_response}"
    );

    assert!(
        get_response.contains("\"url\":\"https://callback.example/a2a\""),
        "{get_response}"
    );

    assert!(
        rejected_response.contains("\"error\""),
        "{rejected_response}"
    );
    assert!(
        rejected_response.contains("Invalid push notification config"),
        "{rejected_response}"
    );

    assert!(
        delete_response.contains("\"result\":null"),
        "{delete_response}"
    );
}

#[test]
fn a2a_server_http_config_persistence_dir_stores_push_configs_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-persistence-config");
    let fallback_config_dir = temp_dir("a2a-persistence-fallback");
    let state_dir = config_dir.join("state");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&fallback_config_dir).expect("fallback config dir should be created");
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!(
            "host: 127.0.0.1\nport: {port}\ntransport: http\npersistence-dir: {}\npush-notifications: true\n",
            state_dir.display()
        ),
    )
    .expect("a2a config should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config"])
        .arg(&config_path)
        .env("IAC_CODE_CONFIG_DIR", &fallback_config_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-persist","method":"SendMessage","params":{{"message":{{"messageId":"msg-persist-push-1","taskId":"task-persist-push-1","contextId":"ctx-persist-push-1","role":"ROLE_USER","parts":[{{"text":"hello from persistence"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let send_response = http_post(port, "/", &send_body);
    let create_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"create-persist","method":"CreateTaskPushNotificationConfig","params":{"taskId":"task-persist-push-1","id":"cfg-1","url":"https://callback.example/a2a"}}"#,
    );
    stop_child(&mut child);

    let empty_owner_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    let expected_config = state_dir
        .join("push_configs")
        .join(empty_owner_hash)
        .join("task-persist-push-1")
        .join("cfg-1.json");
    let fallback_config = fallback_config_dir
        .join("a2a")
        .join("push_configs")
        .join(empty_owner_hash)
        .join("task-persist-push-1")
        .join("cfg-1.json");

    assert!(
        send_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{send_response}"
    );
    assert!(
        create_response.contains("\"id\":\"cfg-1\""),
        "{create_response}"
    );
    assert!(
        expected_config.exists(),
        "push config should be stored under explicit persistence-dir: {}",
        expected_config.display()
    );
    assert!(
        !fallback_config.exists(),
        "push config should not be stored under IAC_CODE_CONFIG_DIR fallback: {}",
        fallback_config.display()
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&fallback_config_dir).ok();
}

#[test]
fn a2a_server_http_push_notifications_enqueue_jobs_like_python() {
    let port = free_tcp_port();
    let config_dir = temp_dir("a2a-push-enqueue-config");
    let state_dir = config_dir.join("state");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!(
            "host: 127.0.0.1\nport: {port}\ntransport: http\npersistence-dir: {}\npush-notifications: true\n",
            state_dir.display()
        ),
    )
    .expect("a2a config should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config"])
        .arg(&config_path)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let _ = wait_for_http_get(port, "/health");

    let first_send = format!(
        r#"{{"jsonrpc":"2.0","id":"send-first","method":"SendMessage","params":{{"message":{{"messageId":"msg-push-first","taskId":"task-push-enqueue","contextId":"ctx-push-enqueue","role":"ROLE_USER","parts":[{{"text":"prepare push task"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let first_response = http_post(port, "/", &first_send);
    let create_response = http_post(
        port,
        "/",
        r#"{"jsonrpc":"2.0","id":"create-push","method":"CreateTaskPushNotificationConfig","params":{"taskId":"task-push-enqueue","id":"cfg-1","url":"https://callback.example/a2a","token":"notify-token","authentication":{"scheme":"bearer","credentials":"callback-secret"}}}"#,
    );
    let second_send = format!(
        r#"{{"jsonrpc":"2.0","id":"send-second","method":"SendMessage","params":{{"message":{{"messageId":"msg-push-second","taskId":"task-push-enqueue","contextId":"ctx-push-enqueue","role":"ROLE_USER","parts":[{{"text":"trigger push job"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"]}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let second_response = http_post(port, "/", &second_send);
    stop_child(&mut child);

    assert!(
        first_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{first_response}"
    );
    assert!(
        create_response.contains("\"id\":\"cfg-1\""),
        "{create_response}"
    );
    assert!(
        second_response.contains("\"state\":\"TASK_STATE_INPUT_REQUIRED\""),
        "{second_response}"
    );

    let mut queue = LocalFileA2APushQueue::new(state_dir.join("push_queue"))
        .with_secret_keyring(A2APushSecretKeyring::new(state_dir.join("push_keys.json")));
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("push job");
    assert_eq!(claimed.task_id, "task-push-enqueue");
    assert_eq!(claimed.config_id, "cfg-1");
    assert_eq!(claimed.url, "https://callback.example/a2a");
    assert!(
        claimed.headers.is_empty(),
        "push job must not persist callback auth headers"
    );
    assert_eq!(
        json_string_path(&claimed.payload, &["statusUpdate", "taskId"]),
        Some("task-push-enqueue")
    );
    assert_eq!(
        json_string_path(&claimed.payload, &["statusUpdate", "contextId"]),
        Some("ctx-push-enqueue")
    );

    fs::remove_dir_all(&config_dir).ok();
}

#[test]
fn a2a_server_stdio_handles_jsonrpc_frames() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--transport", "stdio"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-stdio","method":"SendMessage","params":{{"message":{{"messageId":"msg-stdio-1","taskId":"task-stdio-1","contextId":"ctx-stdio-1","role":"ROLE_USER","parts":[{{"text":"hello over stdio"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let get_body =
        r#"{"jsonrpc":"2.0","id":"get-stdio","method":"GetTask","params":{"id":"task-stdio-1"}}"#;
    let mut stdin = child.stdin.take().expect("stdin is piped");
    writeln!(stdin, "{send_body}").expect("write send frame");
    writeln!(stdin, "{get_body}").expect("write get frame");
    drop(stdin);

    let output = child.wait_with_output().expect("server exits after eof");
    let stdout = String::from_utf8_lossy(&output.stdout);

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    let lines = stdout.lines().collect::<Vec<_>>();
    assert_eq!(lines.len(), 2, "{stdout}");
    assert!(lines[0].contains("\"id\":\"send-stdio\""), "{stdout}");
    assert!(lines[0].contains("\"id\":\"task-stdio-1\""), "{stdout}");
    assert!(
        lines[0].contains("\"state\":\"TASK_STATE_WORKING\""),
        "{stdout}"
    );
    assert!(lines[1].contains("\"id\":\"get-stdio\""), "{stdout}");
    assert!(lines[1].contains("\"id\":\"task-stdio-1\""), "{stdout}");
    assert!(
        lines[1].contains("\"contextId\":\"ctx-stdio-1\""),
        "{stdout}"
    );
}

#[test]
fn acp_server_stdio_handles_initialize_new_session_and_prompt_frames() {
    let config_dir = temp_dir("acp-stdio-server");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = iac_code_command()
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-stdio","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}},"clientInfo":{{"name":"rust-test","version":"1.0"}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let (_init_notifications, init_response) =
        read_acp_until_response(&mut reader, "init-stdio", "initialize");
    assert!(
        init_response.contains("\"protocolVersion\":1"),
        "{init_response}"
    );
    assert!(
        init_response.contains("\"agentInfo\":{\"name\":\"iac-code\""),
        "{init_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-stdio","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-stdio", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");
    assert!(!session_id.is_empty(), "{new_response}");
    let joined_new_notifications = new_notifications.join("");
    assert!(
        joined_new_notifications.contains("\"sessionUpdate\":\"available_commands_update\""),
        "{joined_new_notifications}"
    );
    assert!(
        joined_new_notifications.contains("\"name\":\"clear\""),
        "{joined_new_notifications}"
    );
    assert!(
        joined_new_notifications.contains("\"name\":\"rename\""),
        "{joined_new_notifications}"
    );
    assert!(
        joined_new_notifications.contains("\"hint\":\"<name>\""),
        "{joined_new_notifications}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-stdio","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"hello over acp stdio"}}]}}}}"#
    )
    .expect("write prompt frame");
    stdin.flush().expect("flush prompt frame");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-stdio", "session/prompt");
    let joined_notifications = prompt_notifications.join("");
    assert!(
        joined_notifications.contains("\"method\":\"session/update\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"sessionUpdate\":\"agent_message_chunk\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("fixture response: hello over acp stdio"),
        "{joined_notifications}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_requests_permission_for_tool_calls_before_execution() {
    let config_dir = temp_dir("acp-stdio-permission");
    let workspace_dir = temp_dir("acp-stdio-permission-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let responses = [
            r#"{
                "id": "chatcmpl_permission_tool",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"permission-target.txt\",\"content\":\"should not be written\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
            r#"{
                "id": "chatcmpl_permission_done",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "permission flow done"}
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}
            }"#,
        ];
        let mut requests = Vec::new();
        for body in responses {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("provider request should have read timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write provider response");
            requests.push(request);
        }
        requests
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-perm","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}},"clientInfo":{{"name":"rust-test","version":"1.0"}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-perm", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-perm","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        workspace_dir.display()
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-perm", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-perm","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"write a file"}}]}}}}"#
    )
    .expect("write prompt frame");
    stdin.flush().expect("flush prompt frame");
    let (_pre_permission_notifications, permission_request) =
        read_acp_until_response(&mut reader, "permission-0", "permission request");
    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"permission-0","result":{{"outcome":{{"outcome":"selected","optionId":"reject_once"}}}}}}"#
    )
    .expect("write permission response");
    stdin.flush().expect("flush permission response");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-perm", "session/prompt");
    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    let provider_requests = server.join().expect("provider server finishes");

    let joined_notifications = format!("{permission_request}{}", prompt_notifications.join(""));
    assert!(
        joined_notifications.contains("\"method\":\"session/request_permission\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"toolCall\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"toolCallId\":\"permission/call_write\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"title\":\"write_file\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"optionId\":\"allow_once\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("\"optionId\":\"reject_once\""),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("permission flow done"),
        "{joined_notifications}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );
    assert!(
        provider_requests[0].contains("\"tools\":["),
        "{}",
        provider_requests[0]
    );
    assert!(
        provider_requests[1].contains("Permission denied."),
        "{}",
        provider_requests[1]
    );
    assert!(
        !workspace_dir.join("permission-target.txt").exists(),
        "rejected write_file tool should not create the file"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_honors_client_selected_permission_response() {
    let config_dir = temp_dir("acp-stdio-permission-allow");
    let workspace_dir = temp_dir("acp-stdio-permission-allow-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let responses = [
            r#"{
                "id": "chatcmpl_permission_allow_tool",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_write_allow",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"permission-allowed.txt\",\"content\":\"allowed by client\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
            r#"{
                "id": "chatcmpl_permission_allow_done",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "allowed flow done"}
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3}
            }"#,
        ];
        let mut requests = Vec::new();
        for body in responses {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("provider request should have read timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write provider response");
            requests.push(request);
        }
        requests
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-allow","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}},"clientInfo":{{"name":"rust-test","version":"1.0"}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-allow", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-allow","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        workspace_dir.display()
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-allow", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-allow","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"write an allowed file"}}]}}}}"#
    )
    .expect("write prompt frame");
    stdin.flush().expect("flush prompt frame");
    let (_pre_permission_notifications, permission_request) =
        read_acp_until_response(&mut reader, "permission-0", "permission request");
    assert!(
        permission_request.contains("\"method\":\"session/request_permission\""),
        "{permission_request}"
    );
    assert!(
        permission_request.contains("\"toolCallId\":\"permission/call_write_allow\""),
        "{permission_request}"
    );
    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"permission-0","result":{{"outcome":{{"outcome":"selected","optionId":"allow_once"}}}}}}"#
    )
    .expect("write permission response");
    stdin.flush().expect("flush permission response");

    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-allow", "session/prompt");
    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    let provider_requests = server.join().expect("provider server finishes");

    let joined_notifications = prompt_notifications.join("");
    assert!(
        joined_notifications.contains("Successfully wrote 1 lines"),
        "{joined_notifications}"
    );
    assert!(
        joined_notifications.contains("allowed flow done"),
        "{joined_notifications}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );
    assert!(
        provider_requests[1].contains("Successfully wrote 1 lines"),
        "{}",
        provider_requests[1]
    );
    assert_eq!(
        fs::read_to_string(workspace_dir.join("permission-allowed.txt"))
            .expect("allowed file should be written"),
        "allowed by client"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_cancels_prompt_while_permission_request_is_pending() {
    let config_dir = temp_dir("acp-stdio-permission-cancel");
    let workspace_dir = temp_dir("acp-stdio-permission-cancel-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind provider server");
    listener
        .set_nonblocking(true)
        .expect("provider server should be nonblocking");
    let addr = listener.local_addr().expect("provider server addr");
    let server = thread::spawn(move || {
        let body = r#"{
            "id": "chatcmpl_permission_cancel_tool",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": null,
                    "tool_calls": [{
                        "id": "call_write_cancel",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "{\"path\":\"permission-cancelled.txt\",\"content\":\"should not be written\"}"
                        }
                    }]
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
        }"#;
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("provider request should have read timeout");
        let request = read_http_request(&mut stream);
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write provider response");
        request
    });
    write_openapi_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-cancel","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}},"clientInfo":{{"name":"rust-test","version":"1.0"}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-cancel", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-cancel","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        workspace_dir.display()
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-cancel", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-cancel","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"write then cancel"}}]}}}}"#
    )
    .expect("write prompt frame");
    stdin.flush().expect("flush prompt frame");
    let (_pre_permission_notifications, permission_request) =
        read_acp_until_response(&mut reader, "permission-0", "permission request");
    assert!(
        permission_request.contains("\"toolCallId\":\"permission/call_write_cancel\""),
        "{permission_request}"
    );
    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"cancel-pending","method":"session/cancel","params":{{"sessionId":"{session_id}"}}}}"#
    )
    .expect("write cancel request");
    stdin.flush().expect("flush cancel request");
    let (_cancel_notifications, cancel_response) =
        read_acp_until_response(&mut reader, "cancel-pending", "session/cancel");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-cancel", "session/prompt");
    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    let provider_request = server.join().expect("provider server finishes");

    assert!(
        cancel_response.contains("\"result\":null"),
        "{cancel_response}"
    );
    let joined_notifications = prompt_notifications.join("");
    assert!(
        joined_notifications.contains("Tool execution cancelled."),
        "{joined_notifications}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"cancelled\""),
        "{prompt_response}"
    );
    assert!(
        provider_request.contains("\"content\":\"write then cancel\""),
        "{provider_request}"
    );
    assert!(
        !workspace_dir.join("permission-cancelled.txt").exists(),
        "cancelled write_file tool should not create the file"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_preserves_conversation_between_prompts() {
    let config_dir = temp_dir("acp-stdio-conversation");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = iac_code_command()
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-conv","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-conv", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-conv","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-conv", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-one","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"first"}}]}}}}"#
    )
    .expect("write first prompt");
    stdin.flush().expect("flush first prompt");
    let (first_notifications, first_response) =
        read_acp_until_response(&mut reader, "prompt-one", "first prompt");
    assert!(
        first_notifications
            .join("")
            .contains("conversation messages: 1; last prompt: first"),
        "{}",
        first_notifications.join("")
    );
    assert!(
        first_response.contains("\"stopReason\":\"end_turn\""),
        "{first_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-two","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"second"}}]}}}}"#
    )
    .expect("write second prompt");
    stdin.flush().expect("flush second prompt");
    let (second_notifications, second_response) =
        read_acp_until_response(&mut reader, "prompt-two", "second prompt");
    assert!(
        second_notifications
            .join("")
            .contains("conversation messages: 3; last prompt: second"),
        "{}",
        second_notifications.join("")
    );
    assert!(
        second_response.contains("\"stopReason\":\"end_turn\""),
        "{second_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_memory_slash_reads_configured_memories() {
    let config_dir = temp_dir("acp-stdio-memory");
    let memory_dir = config_dir.join("memory");
    fs::create_dir_all(&memory_dir).expect("memory dir should be created");
    fs::write(
        memory_dir.join("project-note.md"),
        "---\nname: project-note\ndescription: Testing rule\ntype: project\n---\n\nUse fake providers in tests.\n",
    )
    .expect("memory file should be written");

    let mut child = iac_code_command()
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-memory","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-memory", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-memory","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-memory", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"memory-list","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"/memory"}}]}}}}"#
    )
    .expect("write memory list prompt");
    stdin.flush().expect("flush memory list prompt");
    let (list_notifications, list_response) =
        read_acp_until_response(&mut reader, "memory-list", "memory list");
    let list_text = list_notifications.join("");
    assert!(list_text.contains("Saved memories:"), "{list_text}");
    assert!(
        list_text.contains("project-note - Testing rule"),
        "{list_text}"
    );
    assert!(
        list_response.contains("\"stopReason\":\"end_turn\""),
        "{list_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"memory-detail","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"/memory project-note"}}]}}}}"#
    )
    .expect("write memory detail prompt");
    stdin.flush().expect("flush memory detail prompt");
    let (detail_notifications, detail_response) =
        read_acp_until_response(&mut reader, "memory-detail", "memory detail");
    let detail_text = detail_notifications.join("");
    assert!(
        detail_text.contains("[project] Testing rule"),
        "{detail_text}"
    );
    assert!(
        detail_text.contains("Use fake providers in tests."),
        "{detail_text}"
    );
    assert!(
        detail_response.contains("\"stopReason\":\"end_turn\""),
        "{detail_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_compact_slash_uses_session_conversation() {
    let config_dir = temp_dir("acp-stdio-compact");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = iac_code_command()
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-compact","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-compact", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-compact","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-compact", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"compact-empty","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"/compact"}}]}}}}"#
    )
    .expect("write compact prompt");
    stdin.flush().expect("flush compact prompt");
    let (compact_notifications, compact_response) =
        read_acp_until_response(&mut reader, "compact-empty", "compact");
    let compact_text = compact_notifications.join("");
    assert!(
        compact_text.contains("Nothing to compact: conversation is empty."),
        "{compact_text}"
    );
    assert!(
        compact_response.contains("\"stopReason\":\"end_turn\""),
        "{compact_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_rename_slash_updates_session_list_title() {
    let config_dir = temp_dir("acp-stdio-rename");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = iac_code_command()
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-rename","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-rename", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-rename","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new session frame");
    stdin.flush().expect("flush new session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-rename", "session/new");
    let session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"rename-session","method":"session/prompt","params":{{"sessionId":"{session_id}","prompt":[{{"type":"text","text":"/rename main-session"}}]}}}}"#
    )
    .expect("write rename prompt");
    stdin.flush().expect("flush rename prompt");
    let (rename_notifications, rename_response) =
        read_acp_until_response(&mut reader, "rename-session", "rename");
    let rename_text = rename_notifications.join("");
    assert!(
        rename_text.contains("Renamed session to main-session"),
        "{rename_text}"
    );
    assert!(
        rename_response.contains("\"stopReason\":\"end_turn\""),
        "{rename_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"list-renamed","method":"session/list","params":{{}}}}"#
    )
    .expect("write session list frame");
    stdin.flush().expect("flush session list frame");
    let (_list_notifications, list_response) =
        read_acp_until_response(&mut reader, "list-renamed", "session/list");
    assert!(list_response.contains(&session_id), "{list_response}");
    assert!(
        list_response.contains("\"title\":\"main-session\""),
        "{list_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_load_session_replays_persisted_history() {
    let config_dir = temp_dir("acp-stdio-load");
    let cwd = env!("CARGO_MANIFEST_DIR");
    let session_dir = config_dir
        .join("projects")
        .join(sanitize_path(cwd))
        .join("load-session");
    fs::create_dir_all(&session_dir).expect("session dir should be created");
    fs::write(
        session_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"loaded prompt\",\"session_id\":\"load-session\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n{{\"role\":\"assistant\",\"content\":\"loaded answer\",\"session_id\":\"load-session\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n"
        ),
    )
    .expect("session jsonl should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-load","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let (_init_notifications, init_response) =
        read_acp_until_response(&mut reader, "init-load", "initialize");
    assert!(
        init_response.contains("\"loadSession\":true"),
        "{init_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"load-existing","method":"session/load","params":{{"cwd":"{cwd}","sessionId":"load-session"}}}}"#
    )
    .expect("write load session frame");
    stdin.flush().expect("flush load session frame");
    let (load_notifications, load_response) =
        read_acp_until_response(&mut reader, "load-existing", "session/load");
    let replay_text = load_notifications.join("");
    assert!(replay_text.contains("loaded prompt"), "{replay_text}");
    assert!(replay_text.contains("loaded answer"), "{replay_text}");
    assert!(
        load_response.contains("\"currentModelId\""),
        "{load_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-loaded","method":"session/prompt","params":{{"sessionId":"load-session","prompt":[{{"type":"text","text":"next after load"}}]}}}}"#
    )
    .expect("write prompt after load");
    stdin.flush().expect("flush prompt after load");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-loaded", "prompt after load");
    let prompt_text = prompt_notifications.join("");
    assert!(
        prompt_text.contains("conversation messages: 3; last prompt: next after load"),
        "{prompt_text}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_list_sessions_reads_persisted_project_sessions() {
    let config_dir = temp_dir("acp-stdio-list");
    let cwd = env!("CARGO_MANIFEST_DIR");
    let session_dir = config_dir
        .join("projects")
        .join(sanitize_path(cwd))
        .join("listed-session");
    fs::create_dir_all(&session_dir).expect("session dir should be created");
    fs::write(
        session_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"list me\",\"session_id\":\"listed-session\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n"
        ),
    )
    .expect("session jsonl should be written");
    fs::write(
        session_dir.join(SESSION_METADATA_FILENAME),
        format!(
            "{{\"session_id\":\"listed-session\",\"name\":\"deploy-prod\",\"cwd\":\"{cwd}\",\"git_branch\":\"main\",\"schema_version\":1}}\n"
        ),
    )
    .expect("session metadata should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-list","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-list", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"list-persisted","method":"session/list","params":{{"cwd":"{cwd}"}}}}"#
    )
    .expect("write session list frame");
    stdin.flush().expect("flush session list frame");
    let (_notifications, list_response) =
        read_acp_until_response(&mut reader, "list-persisted", "session/list");
    assert!(
        list_response.contains("\"sessionId\":\"listed-session\""),
        "{list_response}"
    );
    assert!(list_response.contains("\"cwd\":\""), "{list_response}");
    assert!(
        list_response.contains("\"title\":\"deploy-prod\""),
        "{list_response}"
    );
    assert!(
        list_response.contains("\"nextCursor\":null"),
        "{list_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_fork_session_inherits_active_history() {
    let config_dir = temp_dir("acp-stdio-fork");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-fork","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-fork", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"new-fork-source","method":"session/new","params":{{"cwd":"{}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write new source session frame");
    stdin.flush().expect("flush new source session frame");
    let (_new_notifications, new_response) =
        read_acp_until_response(&mut reader, "new-fork-source", "session/new");
    let source_session_id = json_string_field(&new_response, "sessionId");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-fork-source","method":"session/prompt","params":{{"sessionId":"{source_session_id}","prompt":[{{"type":"text","text":"source prompt"}}]}}}}"#
    )
    .expect("write source prompt");
    stdin.flush().expect("flush source prompt");
    let (source_notifications, source_response) =
        read_acp_until_response(&mut reader, "prompt-fork-source", "source prompt");
    assert!(
        source_notifications
            .join("")
            .contains("conversation messages: 1; last prompt: source prompt"),
        "{}",
        source_notifications.join("")
    );
    assert!(
        source_response.contains("\"stopReason\":\"end_turn\""),
        "{source_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"fork-active","method":"session/fork","params":{{"cwd":"{}","sessionId":"{source_session_id}"}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    )
    .expect("write fork frame");
    stdin.flush().expect("flush fork frame");
    let (fork_notifications, fork_response) =
        read_acp_until_response(&mut reader, "fork-active", "session/fork");
    let fork_session_id = json_string_field(&fork_response, "sessionId");
    assert_ne!(fork_session_id, source_session_id, "{fork_response}");
    let fork_replay = fork_notifications.join("");
    assert!(fork_replay.contains("source prompt"), "{fork_replay}");
    assert!(
        fork_replay.contains("conversation messages: 1; last prompt: source prompt"),
        "{fork_replay}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-forked","method":"session/prompt","params":{{"sessionId":"{fork_session_id}","prompt":[{{"type":"text","text":"fork follow-up"}}]}}}}"#
    )
    .expect("write fork prompt");
    stdin.flush().expect("flush fork prompt");
    let (fork_prompt_notifications, fork_prompt_response) =
        read_acp_until_response(&mut reader, "prompt-forked", "fork prompt");
    let fork_prompt_text = fork_prompt_notifications.join("");
    assert!(
        fork_prompt_text.contains("conversation messages: 3; last prompt: fork follow-up"),
        "{fork_prompt_text}"
    );
    assert!(
        fork_prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{fork_prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_fork_session_reads_persisted_history() {
    let config_dir = temp_dir("acp-stdio-fork-persisted");
    let cwd = env!("CARGO_MANIFEST_DIR");
    let source_dir = config_dir
        .join("projects")
        .join(sanitize_path(cwd))
        .join("persisted-source");
    fs::create_dir_all(&source_dir).expect("source session dir should be created");
    fs::write(
        source_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"persisted fork prompt\",\"session_id\":\"persisted-source\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n{{\"role\":\"assistant\",\"content\":\"persisted fork answer\",\"session_id\":\"persisted-source\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n"
        ),
    )
    .expect("source session jsonl should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-fork-persisted","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-fork-persisted", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"fork-persisted","method":"session/fork","params":{{"cwd":"{cwd}","sessionId":"persisted-source"}}}}"#
    )
    .expect("write persisted fork frame");
    stdin.flush().expect("flush persisted fork frame");
    let (fork_notifications, fork_response) =
        read_acp_until_response(&mut reader, "fork-persisted", "persisted fork");
    let fork_session_id = json_string_field(&fork_response, "sessionId");
    assert_ne!(fork_session_id, "persisted-source", "{fork_response}");
    let fork_replay = fork_notifications.join("");
    assert!(
        fork_replay.contains("persisted fork prompt"),
        "{fork_replay}"
    );
    assert!(
        fork_replay.contains("persisted fork answer"),
        "{fork_replay}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-persisted-fork","method":"session/prompt","params":{{"sessionId":"{fork_session_id}","prompt":[{{"type":"text","text":"after persisted fork"}}]}}}}"#
    )
    .expect("write prompt after persisted fork");
    stdin.flush().expect("flush prompt after persisted fork");
    let (prompt_notifications, prompt_response) = read_acp_until_response(
        &mut reader,
        "prompt-persisted-fork",
        "persisted fork prompt",
    );
    let prompt_text = prompt_notifications.join("");
    assert!(
        prompt_text.contains("conversation messages: 3; last prompt: after persisted fork"),
        "{prompt_text}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_resume_session_restores_persisted_history() {
    let config_dir = temp_dir("acp-stdio-resume");
    let cwd = env!("CARGO_MANIFEST_DIR");
    let source_dir = config_dir
        .join("projects")
        .join(sanitize_path(cwd))
        .join("resume-source");
    fs::create_dir_all(&source_dir).expect("source session dir should be created");
    fs::write(
        source_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"resume prompt\",\"session_id\":\"resume-source\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n{{\"role\":\"assistant\",\"content\":\"resume answer\",\"session_id\":\"resume-source\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n"
        ),
    )
    .expect("source session jsonl should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-resume","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-resume", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"resume-existing","method":"session/resume","params":{{"cwd":"{cwd}","sessionId":"resume-source"}}}}"#
    )
    .expect("write resume frame");
    stdin.flush().expect("flush resume frame");
    let (_resume_notifications, resume_response) =
        read_acp_until_response(&mut reader, "resume-existing", "session/resume");
    assert!(
        resume_response.contains("\"result\":{}"),
        "{resume_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-resumed","method":"session/prompt","params":{{"sessionId":"resume-source","prompt":[{{"type":"text","text":"after resume"}}]}}}}"#
    )
    .expect("write prompt after resume");
    stdin.flush().expect("flush prompt after resume");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-resumed", "resumed prompt");
    let prompt_text = prompt_notifications.join("");
    assert!(
        prompt_text.contains("conversation messages: 3; last prompt: after resume"),
        "{prompt_text}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[test]
fn acp_server_stdio_resume_session_accepts_persisted_session_name() {
    let config_dir = temp_dir("acp-stdio-resume-name");
    let cwd = env!("CARGO_MANIFEST_DIR");
    let session_dir = config_dir
        .join("projects")
        .join(sanitize_path(cwd))
        .join("stored-named-session");
    fs::create_dir_all(&session_dir).expect("source session dir should be created");
    fs::write(
        session_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"named resume prompt\",\"session_id\":\"stored-named-session\",\"cwd\":\"{cwd}\",\"version\":\"0.4.0\"}}\n"
        ),
    )
    .expect("source session jsonl should be written");
    fs::write(
        session_dir.join(SESSION_METADATA_FILENAME),
        format!(
            "{{\"session_id\":\"stored-named-session\",\"name\":\"deploy-prod\",\"cwd\":\"{cwd}\",\"git_branch\":\"main\",\"created_at\":null,\"updated_at\":null,\"schema_version\":1}}\n"
        ),
    )
    .expect("source session metadata should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["acp", "--transport", "stdio"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "conversation_length")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");
    let mut stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let child_guard = ChildGuard::new(child);
    let mut reader = BufReader::new(stdout);

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"init-resume-name","method":"initialize","params":{{"protocolVersion":1,"clientCapabilities":{{}}}}}}"#
    )
    .expect("write initialize frame");
    stdin.flush().expect("flush initialize frame");
    let _ = read_acp_until_response(&mut reader, "init-resume-name", "initialize");

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"resume-by-name","method":"session/resume","params":{{"cwd":"{cwd}","sessionId":"deploy-prod"}}}}"#
    )
    .expect("write resume frame");
    stdin.flush().expect("flush resume frame");
    let (_resume_notifications, resume_response) =
        read_acp_until_response(&mut reader, "resume-by-name", "session/resume");
    assert!(
        resume_response.contains("\"result\":{}"),
        "{resume_response}"
    );

    writeln!(
        stdin,
        r#"{{"jsonrpc":"2.0","id":"prompt-resumed-name","method":"session/prompt","params":{{"sessionId":"stored-named-session","prompt":[{{"type":"text","text":"after named resume"}}]}}}}"#
    )
    .expect("write prompt after named resume");
    stdin.flush().expect("flush prompt after named resume");
    let (prompt_notifications, prompt_response) =
        read_acp_until_response(&mut reader, "prompt-resumed-name", "resumed prompt");
    let prompt_text = prompt_notifications.join("");
    assert!(
        prompt_text.contains("conversation messages: 2; last prompt: after named resume"),
        "{prompt_text}"
    );
    assert!(
        prompt_response.contains("\"stopReason\":\"end_turn\""),
        "{prompt_response}"
    );

    drop(stdin);
    let status = child_guard.wait();
    let mut stderr_text = String::new();
    stderr
        .read_to_string(&mut stderr_text)
        .expect("stderr is read");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(status.code(), Some(0), "exit code");
    assert_eq!(stderr_text, "", "stderr");
}

#[cfg(unix)]
#[test]
fn a2a_server_unix_handles_jsonrpc_frames() {
    let socket_dir = short_temp_dir("a2a-unix-server");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    let socket_path = socket_dir.join("agent.sock");
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "unix",
            "--socket-path",
            socket_path.to_str().expect("utf8 socket path"),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let mut stream = wait_for_unix_connect(&socket_path, &mut child);
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("set read timeout");
    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-unix","method":"SendMessage","params":{{"message":{{"messageId":"msg-unix-1","taskId":"task-unix-1","contextId":"ctx-unix-1","role":"ROLE_USER","parts":[{{"text":"hello over unix"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    let get_body =
        r#"{"jsonrpc":"2.0","id":"get-unix","method":"GetTask","params":{"id":"task-unix-1"}}"#;
    let subscribe_body = r#"{"jsonrpc":"2.0","id":"subscribe-unix","method":"SubscribeToTask","params":{"id":"task-unix-1"}}"#;
    writeln!(stream, "{send_body}").expect("write send frame");
    writeln!(stream, "{get_body}").expect("write get frame");
    writeln!(stream, "{subscribe_body}").expect("write subscribe frame");
    stream.flush().expect("flush frames");

    let mut reader = BufReader::new(stream);
    let mut send_response = String::new();
    let mut get_response = String::new();
    let mut subscribe_response = String::new();
    reader
        .read_line(&mut send_response)
        .expect("read send response");
    reader
        .read_line(&mut get_response)
        .expect("read get response");
    reader
        .read_line(&mut subscribe_response)
        .expect("read subscribe response");
    stop_child(&mut child);
    fs::remove_dir_all(&socket_dir).ok();

    assert!(
        send_response.contains("\"id\":\"send-unix\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("\"id\":\"task-unix-1\""),
        "{send_response}"
    );
    assert!(
        send_response.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{send_response}"
    );
    assert!(
        get_response.contains("\"id\":\"get-unix\""),
        "{get_response}"
    );
    assert!(
        get_response.contains("\"id\":\"task-unix-1\""),
        "{get_response}"
    );
    assert!(
        get_response.contains("\"contextId\":\"ctx-unix-1\""),
        "{get_response}"
    );
    assert!(
        subscribe_response.contains("\"id\":\"subscribe-unix\""),
        "{subscribe_response}"
    );
    assert!(
        subscribe_response.contains("\"final\":true"),
        "{subscribe_response}"
    );
}

#[test]
fn a2a_server_websocket_handles_jsonrpc_text_frames() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "websocket",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let mut stream = wait_for_websocket_connect(port, &mut child);
    websocket_handshake(&mut stream, port);
    let send_body = format!(
        r#"{{"jsonrpc":"2.0","id":"send-ws","method":"SendMessage","params":{{"message":{{"messageId":"msg-ws-1","taskId":"task-ws-1","contextId":"ctx-ws-1","role":"ROLE_USER","parts":[{{"text":"hello over websocket"}}],"metadata":{{"iac_code":{{"cwd":"{}"}}}}}},"configuration":{{"acceptedOutputModes":["text/plain"],"returnImmediately":true}}}}}}"#,
        env!("CARGO_MANIFEST_DIR")
    );
    write_websocket_text_frame(&mut stream, &send_body);
    let frame = read_websocket_text_frame(&mut stream);
    stop_child(&mut child);

    assert!(frame.contains("\"id\":\"send-ws\""), "{frame}");
    assert!(frame.contains("\"final\":true"), "{frame}");
    assert!(frame.contains("\"payload\""), "{frame}");
    assert!(frame.contains("\"id\":\"task-ws-1\""), "{frame}");
    assert!(
        frame.contains("\"state\":\"TASK_STATE_WORKING\""),
        "{frame}"
    );
}

#[test]
fn a2a_server_grpc_jsonrpc_handles_jsonrpc_envelopes() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "grpc-jsonrpc",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let response = wait_for_grpc_jsonrpc_send(
        port,
        r#"{"jsonrpc":"2.0","id":"card-grpc-jsonrpc","method":"GetExtendedAgentCard","params":{}}"#,
        &mut child,
    );
    stop_child(&mut child);

    assert!(
        response.contains("\"id\":\"card-grpc-jsonrpc\""),
        "{response}"
    );
    assert!(response.contains("\"name\":\"iac-code\""), "{response}");
    assert!(
        response.contains(&format!("\"url\":\"grpc-jsonrpc://127.0.0.1:{port}\"")),
        "{response}"
    );
}

#[test]
fn a2a_server_official_grpc_serves_extended_agent_card() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "grpc",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let card = wait_for_official_grpc_card(port, &mut child);
    stop_child(&mut child);

    assert_eq!(card.name, "iac-code");
    assert_eq!(card.version, env!("CARGO_PKG_VERSION"));
    assert!(
        card.supported_interfaces.iter().any(|interface| {
            interface.url == format!("grpc://127.0.0.1:{port}")
                && interface.protocol_binding == "grpc"
                && interface.protocol_version == "1.0"
        }),
        "{card:?}"
    );
}

#[test]
fn a2a_server_official_grpc_maps_task_and_push_methods() {
    let port = free_tcp_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a",
            "--transport",
            "grpc",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("server starts");

    let _ = wait_for_official_grpc_card(port, &mut child);
    let smoke = run_official_grpc_task_smoke(port);
    stop_child(&mut child);

    assert_eq!(smoke.send_task_id, "task-grpc-1");
    assert_eq!(
        smoke.send_state,
        iac_code_a2a::proto::a2a::TaskState::Working as i32
    );
    assert_eq!(smoke.stream_task_id, "task-grpc-stream-1");
    assert_eq!(smoke.get_context_id, "ctx-grpc-1");
    assert_eq!(smoke.list_total_size, 1);
    assert_eq!(smoke.subscribe_task_id, "task-grpc-1");
    assert_eq!(
        smoke.cancel_state,
        iac_code_a2a::proto::a2a::TaskState::Canceled as i32
    );
    assert_eq!(smoke.push_url, "https://callback.example/a2a");
    assert_eq!(smoke.listed_push_configs, 1);
}

#[test]
fn a2a_server_reports_transport_startup_validation_errors() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--transport", "unix"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "--socket-path is required for --transport unix.\n"
    );
}

#[test]
fn a2a_server_config_reports_missing_push_redis_url_like_python() {
    let config = write_temp_config(
        "a2a-server-missing-push-redis",
        "push-notifications: true\npush-queue: redis-streams\n",
    );
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a", "--config", config.to_str().expect("utf8 path")])
        .output()
        .expect("command runs");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "push-redis-url is required in --config for push-queue: redis-streams.\n"
    );
}

#[test]
fn a2a_client_route_preview_resolves_local_routes_like_python() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "route-preview",
            "--route",
            "template=http://template;skills=iac_generation;tags=ros",
            "--route",
            "review=http://review;skills=iac_review;tags=review",
            "--skill",
            "iac_review",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"name\": \"review\",\n  \"skills\": [\n    \"iac_review\"\n  ],\n  \"tags\": [\n    \"review\"\n  ],\n  \"url\": \"http://review\"\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn a2a_client_route_preview_reads_routes_and_save_options_from_parent_config_like_python() {
    let route_state_dir = temp_dir("a2a-client-route-preview-state");
    let config = write_temp_config(
        "route-preview-config",
        &format!(
            "routes:\n  - name: template\n    url: http://template.example/rpc\n    skills:\n      - iac_generation\n    tags:\n      - ros\n      - template\n  - name: review\n    url: http://review.example/rpc\n    skills:\n      - iac_review\n    tags:\n      - review\nroute-name: template\nroute-state-dir: {}\nsave-routes: true\n",
            route_state_dir.display()
        ),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "route-preview",
        ])
        .output()
        .expect("command runs");
    let saved_routes = fs::read_to_string(route_state_dir.join("routes.json")).unwrap_or_default();
    fs::remove_file(config).ok();
    fs::remove_dir_all(&route_state_dir).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"name\": \"template\",\n  \"skills\": [\n    \"iac_generation\"\n  ],\n  \"tags\": [\n    \"ros\",\n    \"template\"\n  ],\n  \"url\": \"http://template.example/rpc\"\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(
        saved_routes.contains("\"name\": \"template\""),
        "{saved_routes}"
    );
    assert!(
        saved_routes.contains("\"url\": \"http://review.example/rpc\""),
        "{saved_routes}"
    );
}

#[test]
fn a2a_client_route_preview_requires_at_least_one_route() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a-client", "route-preview", "--prompt", "build ros"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "At least one --route is required.\n"
    );
}

#[test]
fn a2a_client_config_requires_yaml_mapping_like_python() {
    let config = write_temp_config("a2a-client-non-mapping-config", "- not-a-mapping\n");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "discover",
        ])
        .output()
        .expect("command runs");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "A2A config file must contain a YAML mapping.\n"
    );
}

#[test]
fn a2a_client_discover_fetches_agent_card_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let mut buffer = [0_u8; 4096];
        let bytes_read = stream.read(&mut buffer).expect("read request");
        let request = String::from_utf8_lossy(&buffer[..bytes_read]).into_owned();
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        request
    });

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "discover",
            "--url",
            &base_url,
            "--token",
            "test-token",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        format!(
            "{{\n  \"name\": \"iac\",\n  \"supportedInterfaces\": [\n    {{\n      \"transport\": \"http\",\n      \"url\": \"{endpoint}\"\n    }}\n  ],\n  \"url\": \"{endpoint}\",\n  \"version\": \"1.0.0\"\n}}\n"
        )
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("GET /.well-known/agent-card.json "));
    assert!(request
        .to_ascii_lowercase()
        .contains("authorization: bearer test-token\r\n"));
}

#[test]
fn a2a_client_discover_requires_url_without_headless_fallback() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a-client", "discover"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "url is required. Provide --url or url in --config.\n"
    );
}

#[test]
fn a2a_client_call_discovers_endpoint_and_prints_response_text() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let card_body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let rpc_body = "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"call ok\"}]}}}}".to_owned();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for body in [card_body, rpc_body] {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            requests.push(request);
        }
        requests
    });

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args([
            "a2a-client",
            "call",
            "--url",
            &base_url,
            "--prompt",
            "hello",
            "--cwd",
            "/tmp/work",
            "--token",
            "test-token",
        ])
        .output()
        .expect("command runs");
    let requests = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "call ok\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(requests[0].starts_with("GET /.well-known/agent-card.json "));
    assert!(requests[1].starts_with("POST /rpc "));
    assert!(requests[1].contains("\"method\":\"SendMessage\""));
    assert!(requests[1].contains("\"text\":\"hello\""));
    assert!(requests[1].contains("\"cwd\":\"/tmp/work\""));
    assert!(requests[1]
        .to_ascii_lowercase()
        .contains("authorization: bearer test-token\r\n"));
}

#[cfg(unix)]
#[test]
fn a2a_client_call_default_cwd_prefers_logical_pwd_like_python() {
    use std::os::unix::fs as unix_fs;

    let root = temp_dir("a2a-client-logical-pwd");
    let physical = root.join("mount-root").join("oss").join("bucket");
    fs::create_dir_all(&physical).expect("physical workspace should be created");
    let logical = root.join("workspace");
    unix_fs::symlink(&physical, &logical).expect("logical workspace symlink should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let card_body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let rpc_body = "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"call ok\"}]}}}}".to_owned();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for body in [card_body, rpc_body] {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            requests.push(request);
        }
        requests
    });

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .current_dir(&logical)
        .env("PWD", &logical)
        .args([
            "a2a-client",
            "call",
            "--url",
            &base_url,
            "--prompt",
            "hello",
        ])
        .output()
        .expect("command runs");
    let requests = server.join().expect("server finishes");
    fs::remove_dir_all(root).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "call ok\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(
        requests[1].contains(&format!(
            "\"cwd\":\"{}\"",
            logical.to_string_lossy().replace('\\', "\\\\")
        )),
        "{}",
        requests[1]
    );
}

#[test]
fn a2a_client_call_uses_discovered_websocket_endpoint() {
    let (ws_url, ws_server) = single_websocket_jsonrpc_server(
        r#"{"id":"call-ws","payload":{"jsonrpc":"2.0","id":"call-ws","result":{"status":{"message":{"role":"ROLE_AGENT","parts":[{"text":"ws call ok"}]}}}},"final":true}"#,
    );
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind card server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let card_body = format!(
        "{{\"url\":\"{ws_url}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"websocket\",\"url\":\"{ws_url}\"}}]}}"
    );
    let card_server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let request = read_http_request(&mut stream);
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            card_body.len(),
            card_body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        request
    });

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "call",
            "--url",
            &base_url,
            "--prompt",
            "hello ws",
            "--cwd",
            "/tmp/work",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "ws call ok\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");

    let card_request = card_server.join().expect("card server finishes");
    let ws_request = ws_server.join().expect("websocket server finishes");
    assert!(card_request.starts_with("GET /.well-known/agent-card.json "));
    assert!(
        ws_request.contains("\"method\":\"SendMessage\""),
        "{ws_request}"
    );
    assert!(ws_request.contains("\"text\":\"hello ws\""), "{ws_request}");
    assert!(ws_request.contains("\"cwd\":\"/tmp/work\""), "{ws_request}");
}

#[test]
fn a2a_client_call_streams_discovered_endpoint_events() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let card_body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let stream_body = "data: {\"result\":{\"text\":\"first\"}}\n\ndata: {\"result\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"second\"}]}}}}\n\ndata: {\"result\":{\"task\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"third\"}]}}}}}\n\n".to_owned();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for (content_type, body) in [
            ("application/json", card_body),
            ("text/event-stream", stream_body),
        ] {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            requests.push(request);
        }
        requests
    });

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "call",
            "--url",
            &base_url,
            "--prompt",
            "hello stream",
            "--cwd",
            "/tmp/work",
            "--stream",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "first\nsecond\nthird\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");

    let requests = server.join().expect("server finishes");
    assert!(requests[0].starts_with("GET /.well-known/agent-card.json "));
    assert!(requests[1].starts_with("POST /rpc "));
    assert!(requests[1].contains("\"method\":\"SendStreamingMessage\""));
    assert!(requests[1].contains("\"text\":\"hello stream\""));
    assert!(requests[1].contains("\"cwd\":\"/tmp/work\""));
}

#[test]
fn a2a_client_call_reads_url_and_auth_from_parent_config_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let card_body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let rpc_body = "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"configured call ok\"}]}}}}".to_owned();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for body in [card_body, rpc_body] {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            requests.push(request);
        }
        requests
    });
    let config = write_temp_config(
        "call",
        &format!(
            "url: {base_url}\napi-key: configured-key\napi-key-header: X-Test-Key\nmodel: qwen3.7-max\n"
        ),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "call",
            "--prompt",
            "hello",
        ])
        .output()
        .expect("command runs");
    let requests = server.join().expect("server finishes");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "configured call ok\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert_eq!(
        header_value(&requests[0], "x-test-key"),
        Some("configured-key")
    );
    assert_eq!(
        header_value(&requests[1], "x-test-key"),
        Some("configured-key")
    );
    assert!(requests[1].contains("\"text\":\"hello\""));
    assert!(requests[1].contains("\"iac_code_model\":\"qwen3.7-max\""));
}

#[test]
fn a2a_client_call_reads_mapping_routes_from_parent_config_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let endpoint = format!("{base_url}/rpc");
    let card_body = format!(
        "{{\"url\":\"{endpoint}\",\"name\":\"iac\",\"version\":\"1.0.0\",\"supportedInterfaces\":[{{\"transport\":\"http\",\"url\":\"{endpoint}\"}}]}}"
    );
    let rpc_body = "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"status\":{\"message\":{\"role\":\"ROLE_AGENT\",\"parts\":[{\"text\":\"route mapping call ok\"}]}}}}".to_owned();
    let server = thread::spawn(move || {
        let mut requests = Vec::new();
        for body in [card_body, rpc_body] {
            let (mut stream, _) = accept_with_timeout(&listener);
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .expect("set timeout");
            let request = read_http_request(&mut stream);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
            requests.push(request);
        }
        requests
    });
    let config = write_temp_config(
        "call-routes-mapping",
        &format!(
            "route-name: template\nroutes:\n  - name: template\n    url: {base_url}\n    skills:\n      - iac_generation\n    tags:\n      - ros\n      - template\n  - name: review\n    url: http://127.0.0.1:1\n    skills:\n      - iac_review\n"
        ),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "call",
            "--prompt",
            "create vpc",
        ])
        .output()
        .expect("command runs");
    let requests = server.join().expect("server finishes");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "route mapping call ok\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(requests[0].starts_with("GET /.well-known/agent-card.json "));
    assert!(requests[1].starts_with("POST /rpc "));
    assert!(requests[1].contains("\"text\":\"create vpc\""));
}

#[test]
fn a2a_client_call_requires_prompt_without_headless_fallback() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args(["a2a-client", "call", "--url", "http://127.0.0.1:1"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "Missing option '--prompt' / '-p'.\n"
    );
}

#[test]
fn a2a_client_call_accepts_equals_prompt_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args(["a2a-client", "call", "--prompt=hello"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("url is required. Provide --url or url in --config"),
        "{stderr}"
    );
    assert!(!stderr.contains("No such option"), "{stderr}");
}

#[test]
fn a2a_client_call_accepts_attached_short_prompt_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .args(["a2a-client", "call", "-phello"])
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("url is required. Provide --url or url in --config"),
        "{stderr}"
    );
    assert!(!stderr.contains("No such option"), "{stderr}");
}

#[test]
fn a2a_client_task_get_reads_parent_config_like_python() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"id\":\"task-from-config\"}}",
    );
    let config = write_temp_config(
        "task-get",
        &format!("url: {url}\ntask-id: task-from-config\napi_key: configured-key\n"),
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "task-get",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"task-from-config\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.contains("\"method\":\"GetTask\""));
    assert!(request.contains("\"id\":\"task-from-config\""));
    assert_eq!(header_value(&request, "x-api-key"), Some("configured-key"));
}

#[test]
fn a2a_client_config_does_not_override_explicit_command_options() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"id\":\"task-from-cli\"}}",
    );
    let config = write_temp_config(
        "override",
        "url: http://127.0.0.1:1\ntask_id: task-from-config\napi_key: configured-key\n",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "--config",
            config.to_str().expect("utf8 path"),
            "task-get",
            "--url",
            &url,
            "--task-id",
            "task-from-cli",
            "--api-key",
            "cli-key",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");
    fs::remove_file(config).ok();

    assert_eq!(output.status.code(), Some(0));
    assert!(request.contains("\"id\":\"task-from-cli\""));
    assert_eq!(header_value(&request, "x-api-key"), Some("cli-key"));
}

#[test]
fn a2a_client_task_get_posts_jsonrpc_and_prints_json() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"id\":\"task-1\",\"status\":{\"state\":\"TASK_STATE_COMPLETED\"}}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-get",
            "--url",
            &url,
            "--task-id",
            "task-1",
            "--history-length",
            "2",
            "--api-key",
            "test-key",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"task-1\",\n    \"status\": {\n      \"state\": \"TASK_STATE_COMPLETED\"\n    }\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"GetTask\""));
    assert!(request.contains("\"id\":\"task-1\""));
    assert!(request.contains("\"historyLength\":2"));
    assert!(request
        .to_ascii_lowercase()
        .contains("x-api-key: test-key\r\n"));
}

#[cfg(unix)]
#[test]
fn a2a_client_task_get_posts_jsonrpc_over_unix_socket() {
    let (url, server, socket_dir) = single_unix_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-unix\",\"result\":{\"id\":\"task-unix-client\",\"status\":{\"state\":\"TASK_STATE_COMPLETED\"}}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-get",
            "--url",
            &url,
            "--task-id",
            "task-unix-client",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");
    fs::remove_dir_all(&socket_dir).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-unix\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"task-unix-client\",\n    \"status\": {\n      \"state\": \"TASK_STATE_COMPLETED\"\n    }\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.contains("\"method\":\"GetTask\""), "{request}");
    assert!(request.contains("\"id\":\"task-unix-client\""), "{request}");
}

#[test]
fn a2a_client_task_get_posts_jsonrpc_over_websocket() {
    let (url, server) = single_websocket_jsonrpc_server(
        r#"{"id":"rpc-ws","payload":{"jsonrpc":"2.0","id":"rpc-ws","result":{"id":"task-ws-client"}},"final":true}"#,
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-get",
            "--url",
            &url,
            "--task-id",
            "task-ws-client",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-ws\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"task-ws-client\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");

    let request = server.join().expect("server finishes");
    assert!(request.contains("\"method\":\"GetTask\""), "{request}");
    assert!(request.contains("\"id\":\"task-ws-client\""), "{request}");
}

#[test]
fn a2a_client_task_list_json_posts_filters_and_prints_json() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"tasks\":[{\"id\":\"task-1\"}],\"totalSize\":1}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-list",
            "--url",
            &url,
            "--context-id",
            "ctx-1",
            "--status",
            "TASK_STATE_WORKING",
            "--page-size",
            "10",
            "--page-token",
            "next",
            "--include-artifacts",
            "--output",
            "json",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"tasks\": [\n      {\n        \"id\": \"task-1\"\n      }\n    ],\n    \"totalSize\": 1\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"ListTasks\""));
    assert!(request.contains("\"contextId\":\"ctx-1\""));
    assert!(request.contains("\"status\":\"TASK_STATE_WORKING\""));
    assert!(request.contains("\"pageSize\":10"));
    assert!(request.contains("\"pageToken\":\"next\""));
    assert!(request.contains("\"includeArtifacts\":true"));
}

#[test]
fn a2a_client_task_cancel_posts_jsonrpc_and_prints_json() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"id\":\"task-1\",\"status\":{\"state\":\"TASK_STATE_CANCELED\"}}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-cancel",
            "--url",
            &url,
            "--task-id",
            "task-1",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"task-1\",\n    \"status\": {\n      \"state\": \"TASK_STATE_CANCELED\"\n    }\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"CancelTask\""));
    assert!(request.contains("\"id\":\"task-1\""));
}

#[test]
fn a2a_client_task_subscribe_posts_jsonrpc_and_prints_sse_events() {
    let (url, server) = single_sse_server(
        ": keepalive\n\n\
data: {\"taskId\":\"task-1\",\"event\":\"started\"}\n\n\
event: ignored\n\
data: {\"final\":true,\"event\":\"done\"}\n\n",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-subscribe",
            "--url",
            &url,
            "--task-id",
            "task-1",
            "--api-key",
            "test-key",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\"event\":\"started\",\"taskId\":\"task-1\"}\n{\"event\":\"done\",\"final\":true}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"SubscribeToTask\""));
    assert!(request.contains("\"id\":\"task-1\""));
    assert_eq!(header_value(&request, "x-api-key"), Some("test-key"));
}

#[cfg(unix)]
#[test]
fn a2a_client_task_subscribe_streams_jsonrpc_over_unix_socket() {
    let (url, server, socket_dir) = single_unix_stream_server(&[
        r#"{"taskId":"task-unix-stream","event":"started"}"#,
        r#"{"final":true,"event":"done"}"#,
    ]);

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-subscribe",
            "--url",
            &url,
            "--task-id",
            "task-unix-stream",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");
    fs::remove_dir_all(&socket_dir).ok();

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\"event\":\"started\",\"taskId\":\"task-unix-stream\"}\n{\"event\":\"done\",\"final\":true}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(
        request.contains("\"method\":\"SubscribeToTask\""),
        "{request}"
    );
    assert!(request.contains("\"id\":\"task-unix-stream\""), "{request}");
}

#[test]
fn a2a_client_task_subscribe_streams_jsonrpc_over_websocket() {
    let (url, server) = single_websocket_stream_server(&[
        r#"{"id":"subscribe-ws","payload":{"taskId":"task-ws-stream","event":"started"},"final":false}"#,
        r#"{"id":"subscribe-ws","payload":{"final":true,"event":"done"},"final":true}"#,
    ]);

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-subscribe",
            "--url",
            &url,
            "--task-id",
            "task-ws-stream",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\"event\":\"started\",\"taskId\":\"task-ws-stream\"}\n{\"event\":\"done\",\"final\":true}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");

    let request = server.join().expect("server finishes");
    assert!(
        request.contains("\"method\":\"SubscribeToTask\""),
        "{request}"
    );
    assert!(request.contains("\"id\":\"task-ws-stream\""), "{request}");
}

#[test]
fn a2a_client_task_subscribe_requires_task_id_without_headless_fallback() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "task-subscribe",
            "--url",
            "http://127.0.0.1:1",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "task-id is required. Provide --task-id or task-id in --config.\n"
    );
}

#[test]
fn a2a_client_task_get_requires_task_id_without_headless_fallback() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["a2a-client", "task-get", "--url", "http://127.0.0.1:1"])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "task-id is required. Provide --task-id or task-id in --config.\n"
    );
}

#[test]
fn a2a_client_extended_card_posts_jsonrpc_and_prints_json() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"name\":\"iac\",\"version\":\"extended\"}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "extended-card",
            "--url",
            &url,
            "--token",
            "test-token",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"name\": \"iac\",\n    \"version\": \"extended\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"GetExtendedAgentCard\""));
    assert!(request.contains("\"params\":{}"));
    assert!(request
        .to_ascii_lowercase()
        .contains("authorization: bearer test-token\r\n"));
}

#[test]
fn a2a_client_push_config_create_posts_jsonrpc_and_prints_json() {
    let (url, server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-1\",\"result\":{\"id\":\"cfg-1\",\"taskId\":\"task-1\",\"url\":\"https://callback.example/a2a\"}}",
    );

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "push-config-create",
            "--url",
            &url,
            "--task-id",
            "task-1",
            "--config-id",
            "cfg-1",
            "--callback-url",
            "https://callback.example/a2a",
            "--notification-token",
            "notify-token",
            "--auth-scheme",
            "bearer",
            "--auth-credentials",
            "callback-secret",
            "--basic-username",
            "user",
            "--basic-password",
            "pass",
        ])
        .output()
        .expect("command runs");
    let request = server.join().expect("server finishes");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "{\n  \"id\": \"rpc-1\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"cfg-1\",\n    \"taskId\": \"task-1\",\n    \"url\": \"https://callback.example/a2a\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
    assert!(request.starts_with("POST / "));
    assert!(request.contains("\"method\":\"CreateTaskPushNotificationConfig\""));
    assert!(request.contains("\"taskId\":\"task-1\""));
    assert!(request.contains("\"id\":\"cfg-1\""));
    assert!(request.contains("\"url\":\"https://callback.example/a2a\""));
    assert!(request.contains("\"token\":\"notify-token\""));
    assert!(request.contains(
        "\"authentication\":{\"credentials\":\"callback-secret\",\"scheme\":\"bearer\"}"
    ));
    assert_eq!(
        header_value(&request, "authorization"),
        Some("Basic dXNlcjpwYXNz")
    );
}

#[test]
fn a2a_client_push_config_get_list_delete_post_jsonrpc_and_print_json() {
    let (get_url, get_server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-get\",\"result\":{\"id\":\"cfg-1\",\"taskId\":\"task-1\"}}",
    );
    let get_output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "push-config-get",
            "--url",
            &get_url,
            "--task-id",
            "task-1",
            "--config-id",
            "cfg-1",
        ])
        .output()
        .expect("command runs");
    let get_request = get_server.join().expect("server finishes");

    assert_eq!(get_output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&get_output.stdout),
        "{\n  \"id\": \"rpc-get\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"id\": \"cfg-1\",\n    \"taskId\": \"task-1\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&get_output.stderr), "");
    assert!(get_request.contains("\"method\":\"GetTaskPushNotificationConfig\""));
    assert!(get_request.contains("\"taskId\":\"task-1\""));
    assert!(get_request.contains("\"id\":\"cfg-1\""));

    let (list_url, list_server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-list\",\"result\":{\"configs\":[{\"id\":\"cfg-1\"}],\"nextPageToken\":\"next\"}}",
    );
    let list_output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "push-config-list",
            "--url",
            &list_url,
            "--task-id",
            "task-1",
            "--page-size",
            "10",
            "--page-token",
            "next",
        ])
        .output()
        .expect("command runs");
    let list_request = list_server.join().expect("server finishes");

    assert_eq!(list_output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&list_output.stdout),
        "{\n  \"id\": \"rpc-list\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"configs\": [\n      {\n        \"id\": \"cfg-1\"\n      }\n    ],\n    \"nextPageToken\": \"next\"\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&list_output.stderr), "");
    assert!(list_request.contains("\"method\":\"ListTaskPushNotificationConfigs\""));
    assert!(list_request.contains("\"taskId\":\"task-1\""));
    assert!(list_request.contains("\"pageSize\":10"));
    assert!(list_request.contains("\"pageToken\":\"next\""));

    let (delete_url, delete_server) = single_jsonrpc_server(
        "{\"jsonrpc\":\"2.0\",\"id\":\"rpc-delete\",\"result\":{\"deleted\":true}}",
    );
    let delete_output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "push-config-delete",
            "--url",
            &delete_url,
            "--task-id",
            "task-1",
            "--config-id",
            "cfg-1",
        ])
        .output()
        .expect("command runs");
    let delete_request = delete_server.join().expect("server finishes");

    assert_eq!(delete_output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8_lossy(&delete_output.stdout),
        "{\n  \"id\": \"rpc-delete\",\n  \"jsonrpc\": \"2.0\",\n  \"result\": {\n    \"deleted\": true\n  }\n}\n"
    );
    assert_eq!(String::from_utf8_lossy(&delete_output.stderr), "");
    assert!(delete_request.contains("\"method\":\"DeleteTaskPushNotificationConfig\""));
    assert!(delete_request.contains("\"taskId\":\"task-1\""));
    assert!(delete_request.contains("\"id\":\"cfg-1\""));
}

#[test]
fn a2a_client_push_config_create_requires_callback_url_without_headless_fallback() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "a2a-client",
            "push-config-create",
            "--url",
            "http://127.0.0.1:1",
            "--task-id",
            "task-1",
            "--config-id",
            "cfg-1",
        ])
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "callback-url is required. Provide --callback-url or callback-url in --config.\n"
    );
}

fn single_jsonrpc_server(body: &'static str) -> (String, thread::JoinHandle<String>) {
    single_response_server("application/json", body)
}

fn single_sse_server(body: &'static str) -> (String, thread::JoinHandle<String>) {
    single_response_server("text/event-stream", body)
}

fn single_websocket_jsonrpc_server(body: &'static str) -> (String, thread::JoinHandle<String>) {
    single_websocket_server(vec![body])
}

fn single_websocket_stream_server(
    bodies: &'static [&'static str],
) -> (String, thread::JoinHandle<String>) {
    single_websocket_server(bodies.to_vec())
}

fn single_websocket_server(bodies: Vec<&'static str>) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind websocket server");
    let url = format!("ws://{}/a2a", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let handshake = read_http_request(&mut stream);
        let accept = websocket_accept_header(&handshake);
        stream
            .write_all(
                format!(
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                .as_bytes(),
            )
            .expect("write handshake");
        let request = read_websocket_text_frame(&mut stream);
        for body in bodies {
            write_websocket_server_text_frame(&mut stream, body);
        }
        request
    });
    (url, server)
}

#[cfg(unix)]
fn single_unix_jsonrpc_server(body: &'static str) -> (String, thread::JoinHandle<String>, PathBuf) {
    let socket_dir = short_temp_dir("a2a-client-unix");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    let socket_path = socket_dir.join("agent.sock");
    let listener = std::os::unix::net::UnixListener::bind(&socket_path).expect("bind unix server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let url = format!("unix://{}", socket_path.display());
    let server = thread::spawn(move || {
        let stream = accept_unix_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
        let mut request = String::new();
        reader.read_line(&mut request).expect("read request frame");
        let mut writer = stream;
        writeln!(writer, "{body}").expect("write response frame");
        request
    });
    (url, server, socket_dir)
}

#[cfg(unix)]
fn single_unix_stream_server(
    bodies: &'static [&'static str],
) -> (String, thread::JoinHandle<String>, PathBuf) {
    let socket_dir = short_temp_dir("a2a-client-unix-stream");
    fs::create_dir_all(&socket_dir).expect("socket dir");
    let socket_path = socket_dir.join("agent.sock");
    let listener = std::os::unix::net::UnixListener::bind(&socket_path).expect("bind unix server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let url = format!("unix://{}", socket_path.display());
    let server = thread::spawn(move || {
        let stream = accept_unix_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
        let mut request = String::new();
        reader.read_line(&mut request).expect("read request frame");
        let mut writer = stream;
        for body in bodies {
            writeln!(writer, "{body}").expect("write response frame");
        }
        request
    });
    (url, server, socket_dir)
}

#[cfg(unix)]
fn accept_unix_with_timeout(
    listener: &std::os::unix::net::UnixListener,
) -> std::os::unix::net::UnixStream {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                stream
                    .set_nonblocking(false)
                    .expect("set unix stream blocking");
                return stream;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    panic!("timed out waiting for unix request");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
            Err(error) => panic!("accept unix request: {error}"),
        }
    }
}

fn single_response_server(
    content_type: &'static str,
    body: &'static str,
) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let url = format!("http://{}", listener.local_addr().expect("local addr"));
    listener.set_nonblocking(true).expect("set nonblocking");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(&listener);
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let request = read_http_request(&mut stream);
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        request
    });
    (url, server)
}

fn accept_with_timeout(listener: &TcpListener) -> (std::net::TcpStream, std::net::SocketAddr) {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    loop {
        match listener.accept() {
            Ok((stream, address)) => {
                stream.set_nonblocking(false).expect("set blocking");
                return (stream, address);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    panic!("timed out waiting for request");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
            Err(error) => panic!("accept request: {error}"),
        }
    }
}

fn free_tcp_port() -> u16 {
    let base = 20_000 + (std::process::id() % 20_000) as u16;
    for _ in 0..20_000 {
        let offset = NEXT_TEST_PORT_OFFSET.fetch_add(1, Ordering::Relaxed);
        let port = 20_000 + (base.wrapping_add(offset) % 40_000);
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return port;
        }
    }
    TcpListener::bind("127.0.0.1:0")
        .expect("bind fallback free port")
        .local_addr()
        .expect("local addr")
        .port()
}

fn wait_for_http_get(port: u16, path: &str) -> String {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    loop {
        match try_http_get(port, path) {
            Ok(response) => return response,
            Err(error) => {
                if Instant::now() >= deadline {
                    panic!("timed out waiting for a2a server: {error}");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
        }
    }
}

fn wait_for_websocket_connect(port: u16, child: &mut Child) -> std::net::TcpStream {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    loop {
        match std::net::TcpStream::connect(("127.0.0.1", port)) {
            Ok(stream) => {
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .expect("set read timeout");
                return stream;
            }
            Err(error) => {
                if let Some(status) = child.try_wait().expect("check child status") {
                    let stderr = child
                        .stderr
                        .take()
                        .map(|mut stderr| {
                            let mut text = String::new();
                            stderr.read_to_string(&mut text).ok();
                            text
                        })
                        .unwrap_or_default();
                    panic!("websocket server exited before it was ready: {status}; {stderr}");
                }
                if Instant::now() >= deadline {
                    panic!("timed out waiting for websocket server: {error}");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
        }
    }
}

fn wait_for_grpc_jsonrpc_send(port: u16, body: &str, child: &mut Child) -> String {
    let endpoint = format!("http://127.0.0.1:{port}");
    let payload = body.as_bytes().to_vec();
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    loop {
        let result = runtime.block_on(async {
            let mut client =
                iac_code_a2a::proto::grpc_jsonrpc::a2a_json_rpc_client::A2aJsonRpcClient::connect(
                    endpoint.clone(),
                )
                .await
                .map_err(|error| error.to_string())?;
            let response = client
                .send(iac_code_a2a::proto::grpc_jsonrpc::JsonRpcEnvelope {
                    payload: payload.clone(),
                    r#final: false,
                })
                .await
                .map_err(|error| error.to_string())?;
            Ok::<_, String>(response.into_inner())
        });
        match result {
            Ok(response) => {
                return String::from_utf8(response.payload).expect("utf8 gRPC payload");
            }
            Err(error) => {
                if let Some(status) = child.try_wait().expect("check child status") {
                    let stderr = child
                        .stderr
                        .take()
                        .map(|mut stderr| {
                            let mut text = String::new();
                            stderr.read_to_string(&mut text).ok();
                            text
                        })
                        .unwrap_or_default();
                    panic!("gRPC JSON-RPC server exited before it was ready: {status}; {stderr}");
                }
                if Instant::now() >= deadline {
                    panic!("timed out waiting for gRPC JSON-RPC server: {error}");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
        }
    }
}

fn wait_for_official_grpc_card(
    port: u16,
    child: &mut Child,
) -> iac_code_a2a::proto::a2a::AgentCard {
    let endpoint = format!("http://127.0.0.1:{port}");
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    loop {
        let result = runtime.block_on(async {
            let mut client =
                iac_code_a2a::proto::a2a::a2a_service_client::A2aServiceClient::connect(
                    endpoint.clone(),
                )
                .await
                .map_err(|error| error.to_string())?;
            let response = client
                .get_extended_agent_card(iac_code_a2a::proto::a2a::GetExtendedAgentCardRequest {
                    tenant: String::new(),
                })
                .await
                .map_err(|error| error.to_string())?;
            Ok::<_, String>(response.into_inner())
        });
        match result {
            Ok(card) => return card,
            Err(error) => {
                if let Some(status) = child.try_wait().expect("check child status") {
                    let stderr = child
                        .stderr
                        .take()
                        .map(|mut stderr| {
                            let mut text = String::new();
                            stderr.read_to_string(&mut text).ok();
                            text
                        })
                        .unwrap_or_default();
                    panic!("official gRPC server exited before it was ready: {status}; {stderr}");
                }
                if Instant::now() >= deadline {
                    panic!("timed out waiting for official gRPC server: {error}");
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
        }
    }
}

#[derive(Debug)]
struct OfficialGrpcTaskSmoke {
    send_task_id: String,
    send_state: i32,
    stream_task_id: String,
    get_context_id: String,
    list_total_size: i32,
    subscribe_task_id: String,
    cancel_state: i32,
    push_url: String,
    listed_push_configs: usize,
}

fn run_official_grpc_task_smoke(port: u16) -> OfficialGrpcTaskSmoke {
    let endpoint = format!("http://127.0.0.1:{port}");
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    runtime.block_on(async move {
        let mut client =
            iac_code_a2a::proto::a2a::a2a_service_client::A2aServiceClient::connect(endpoint)
                .await
                .expect("official grpc client connects");
        let send = client
            .send_message(iac_code_a2a::proto::a2a::SendMessageRequest {
                tenant: String::new(),
                message: Some(iac_code_a2a::proto::a2a::Message {
                    message_id: "msg-grpc-1".to_owned(),
                    context_id: "ctx-grpc-1".to_owned(),
                    task_id: "task-grpc-1".to_owned(),
                    role: iac_code_a2a::proto::a2a::Role::User as i32,
                    parts: vec![iac_code_a2a::proto::a2a::Part {
                        content: Some(iac_code_a2a::proto::a2a::part::Content::Text(
                            "hello over official grpc".to_owned(),
                        )),
                        ..Default::default()
                    }],
                    ..Default::default()
                }),
                configuration: Some(iac_code_a2a::proto::a2a::SendMessageConfiguration {
                    return_immediately: true,
                    accepted_output_modes: vec!["text/plain".to_owned()],
                    ..Default::default()
                }),
                metadata: None,
            })
            .await
            .expect("send message")
            .into_inner();
        let send_task = match send.payload.expect("send payload") {
            iac_code_a2a::proto::a2a::send_message_response::Payload::Task(task) => task,
            other => panic!("unexpected send payload: {other:?}"),
        };
        let send_state = send_task.status.as_ref().expect("send status").state;
        let mut send_stream = client
            .send_streaming_message(iac_code_a2a::proto::a2a::SendMessageRequest {
                tenant: String::new(),
                message: Some(iac_code_a2a::proto::a2a::Message {
                    message_id: "msg-grpc-stream-1".to_owned(),
                    context_id: "ctx-grpc-stream-1".to_owned(),
                    task_id: "task-grpc-stream-1".to_owned(),
                    role: iac_code_a2a::proto::a2a::Role::User as i32,
                    parts: vec![iac_code_a2a::proto::a2a::Part {
                        content: Some(iac_code_a2a::proto::a2a::part::Content::Text(
                            "hello over official grpc stream".to_owned(),
                        )),
                        ..Default::default()
                    }],
                    ..Default::default()
                }),
                configuration: Some(iac_code_a2a::proto::a2a::SendMessageConfiguration {
                    return_immediately: true,
                    accepted_output_modes: vec!["text/plain".to_owned()],
                    ..Default::default()
                }),
                metadata: None,
            })
            .await
            .expect("send streaming message")
            .into_inner();
        let stream_task_id = match send_stream
            .message()
            .await
            .expect("send stream read")
            .expect("send stream response")
            .payload
            .expect("send stream payload")
        {
            iac_code_a2a::proto::a2a::stream_response::Payload::Task(task) => task.id,
            other => panic!("unexpected send stream payload: {other:?}"),
        };

        let get = client
            .get_task(iac_code_a2a::proto::a2a::GetTaskRequest {
                tenant: String::new(),
                id: "task-grpc-1".to_owned(),
                history_length: None,
            })
            .await
            .expect("get task")
            .into_inner();
        let list = client
            .list_tasks(iac_code_a2a::proto::a2a::ListTasksRequest {
                tenant: String::new(),
                context_id: "ctx-grpc-1".to_owned(),
                status: iac_code_a2a::proto::a2a::TaskState::Unspecified as i32,
                page_size: Some(10),
                page_token: String::new(),
                history_length: None,
                status_timestamp_after: None,
                include_artifacts: Some(true),
            })
            .await
            .expect("list tasks")
            .into_inner();
        let mut stream = client
            .subscribe_to_task(iac_code_a2a::proto::a2a::SubscribeToTaskRequest {
                tenant: String::new(),
                id: "task-grpc-1".to_owned(),
            })
            .await
            .expect("subscribe task")
            .into_inner();
        let subscribe = stream
            .message()
            .await
            .expect("subscribe stream read")
            .expect("subscribe response");
        let subscribe_task_id = match subscribe.payload.expect("subscribe payload") {
            iac_code_a2a::proto::a2a::stream_response::Payload::Task(task) => task.id,
            other => panic!("unexpected subscribe payload: {other:?}"),
        };
        let cancel = client
            .cancel_task(iac_code_a2a::proto::a2a::CancelTaskRequest {
                tenant: String::new(),
                id: "task-grpc-1".to_owned(),
                metadata: None,
            })
            .await
            .expect("cancel task")
            .into_inner();
        let push = client
            .create_task_push_notification_config(
                iac_code_a2a::proto::a2a::TaskPushNotificationConfig {
                    tenant: String::new(),
                    id: "cfg-grpc-1".to_owned(),
                    task_id: "task-grpc-1".to_owned(),
                    url: "https://callback.example/a2a".to_owned(),
                    token: "push-token".to_owned(),
                    authentication: Some(iac_code_a2a::proto::a2a::AuthenticationInfo {
                        scheme: "bearer".to_owned(),
                        credentials: "push-secret".to_owned(),
                    }),
                },
            )
            .await
            .expect("create push config")
            .into_inner();
        let _get_push = client
            .get_task_push_notification_config(
                iac_code_a2a::proto::a2a::GetTaskPushNotificationConfigRequest {
                    tenant: String::new(),
                    task_id: "task-grpc-1".to_owned(),
                    id: "cfg-grpc-1".to_owned(),
                },
            )
            .await
            .expect("get push config")
            .into_inner();
        let listed_push = client
            .list_task_push_notification_configs(
                iac_code_a2a::proto::a2a::ListTaskPushNotificationConfigsRequest {
                    tenant: String::new(),
                    task_id: "task-grpc-1".to_owned(),
                    page_size: 10,
                    page_token: String::new(),
                },
            )
            .await
            .expect("list push configs")
            .into_inner();
        client
            .delete_task_push_notification_config(
                iac_code_a2a::proto::a2a::DeleteTaskPushNotificationConfigRequest {
                    tenant: String::new(),
                    task_id: "task-grpc-1".to_owned(),
                    id: "cfg-grpc-1".to_owned(),
                },
            )
            .await
            .expect("delete push config");

        OfficialGrpcTaskSmoke {
            send_task_id: send_task.id,
            send_state,
            stream_task_id,
            get_context_id: get.context_id,
            list_total_size: list.total_size,
            subscribe_task_id,
            cancel_state: cancel.status.expect("cancel status").state,
            push_url: push.url,
            listed_push_configs: listed_push.configs.len(),
        }
    })
}

fn websocket_handshake(stream: &mut std::net::TcpStream, port: u16) {
    stream
        .write_all(
            format!(
                "GET /a2a HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
            )
            .as_bytes(),
        )
        .expect("write handshake");
    let mut response = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let count = stream.read(&mut buffer).expect("read handshake");
        assert!(count > 0, "websocket handshake closed");
        response.extend_from_slice(&buffer[..count]);
        if response.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let response = String::from_utf8_lossy(&response);
    assert!(response.starts_with("HTTP/1.1 101 "), "{response}");
    assert!(
        response.contains("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo="),
        "{response}"
    );
}

fn write_websocket_text_frame(stream: &mut std::net::TcpStream, text: &str) {
    let payload = text.as_bytes();
    let mut frame = Vec::from([0x81_u8]);
    if payload.len() < 126 {
        frame.push(0x80 | payload.len() as u8);
    } else {
        frame.push(0x80 | 126);
        frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    }
    let mask = [1_u8, 2, 3, 4];
    frame.extend_from_slice(&mask);
    frame.extend(
        payload
            .iter()
            .enumerate()
            .map(|(index, byte)| byte ^ mask[index % mask.len()]),
    );
    stream.write_all(&frame).expect("write websocket frame");
}

fn read_websocket_text_frame(stream: &mut std::net::TcpStream) -> String {
    let mut header = [0_u8; 2];
    stream.read_exact(&mut header).expect("read frame header");
    assert_eq!(header[0] & 0x0f, 0x1, "expected text frame");
    let masked = header[1] & 0x80 != 0;
    let mut length = (header[1] & 0x7f) as usize;
    if length == 126 {
        let mut extended = [0_u8; 2];
        stream.read_exact(&mut extended).expect("read frame length");
        length = u16::from_be_bytes(extended) as usize;
    } else if length == 127 {
        let mut extended = [0_u8; 8];
        stream.read_exact(&mut extended).expect("read frame length");
        length = u64::from_be_bytes(extended) as usize;
    }
    let mut mask = [0_u8; 4];
    if masked {
        stream.read_exact(&mut mask).expect("read frame mask");
    }
    let mut payload = vec![0_u8; length];
    stream.read_exact(&mut payload).expect("read frame payload");
    if masked {
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % mask.len()];
        }
    }
    String::from_utf8(payload).expect("websocket text frame should be utf8")
}

fn write_websocket_server_text_frame(stream: &mut std::net::TcpStream, text: &str) {
    let payload = text.as_bytes();
    let mut frame = Vec::from([0x81_u8]);
    if payload.len() < 126 {
        frame.push(payload.len() as u8);
    } else {
        frame.push(126);
        frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    }
    frame.extend_from_slice(payload);
    stream.write_all(&frame).expect("write websocket frame");
}

#[cfg(unix)]
fn wait_for_unix_connect(path: &Path, child: &mut Child) -> std::os::unix::net::UnixStream {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    loop {
        match std::os::unix::net::UnixStream::connect(path) {
            Ok(stream) => return stream,
            Err(error) => {
                if let Some(status) = child.try_wait().expect("check child status") {
                    panic!("unix server exited before socket was ready: {status}");
                }
                if Instant::now() >= deadline {
                    panic!(
                        "timed out waiting for unix socket {}: {error}",
                        path.display()
                    );
                }
                thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
            }
        }
    }
}

fn http_get(port: u16, path: &str) -> String {
    try_http_get(port, path).expect("http get")
}

fn http_post(port: u16, path: &str, body: &str) -> String {
    http_post_with_headers(port, path, body, &[])
}

fn http_post_with_headers(port: u16, path: &str, body: &str, headers: &[(&str, &str)]) -> String {
    http_request_with_headers(port, "POST", path, body, headers)
}

fn http_get_with_headers(port: u16, path: &str, headers: &[(&str, &str)]) -> String {
    http_request_with_headers(port, "GET", path, "", headers)
}

fn http_request_with_headers(
    port: u16,
    method: &str,
    path: &str,
    body: &str,
    headers: &[(&str, &str)],
) -> String {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).expect("connect");
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("set timeout");
    let mut request = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n");
    if method == "POST" {
        request.push_str("Content-Type: application/json\r\n");
    }
    for (key, value) in headers {
        request.push_str(key);
        request.push_str(": ");
        request.push_str(value);
        request.push_str("\r\n");
    }
    request.push_str(&format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    ));
    stream.write_all(request.as_bytes()).expect("write request");
    let mut response = String::new();
    stream.read_to_string(&mut response).expect("read response");
    response
}

fn http_response_header(response: &str, name: &str) -> Option<String> {
    response
        .lines()
        .take_while(|line| !line.is_empty())
        .find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.eq_ignore_ascii_case(name)
                .then(|| value.trim().to_owned())
        })
}

fn wait_for_acp_http_sse_contains(port: u16, conn_id: &str, expected: &str) -> String {
    let deadline = Instant::now() + TEST_SERVER_ACCEPT_TIMEOUT;
    let mut accumulated = String::new();
    loop {
        let response = http_get_with_headers(port, "/acp", &[("Acp-Connection-Id", conn_id)]);
        accumulated.push_str(&response);
        if accumulated.contains(expected) {
            return accumulated;
        }
        if Instant::now() >= deadline {
            panic!("timed out waiting for ACP HTTP SSE fragment {expected}: {accumulated}");
        }
        thread::sleep(TEST_SERVER_ACCEPT_POLL_INTERVAL);
    }
}

fn try_http_get(port: u16, path: &str) -> std::io::Result<String> {
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.write_all(
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
            .as_bytes(),
    )?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    Ok(response)
}

fn stop_child(child: &mut Child) {
    child.kill().ok();
    child.wait().ok();
}

struct ChildGuard {
    child: Option<Child>,
}

impl ChildGuard {
    fn new(child: Child) -> Self {
        Self { child: Some(child) }
    }

    fn wait(mut self) -> std::process::ExitStatus {
        self.child
            .take()
            .expect("child is present")
            .wait()
            .expect("child exits")
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            child.kill().ok();
            child.wait().ok();
        }
    }
}

fn read_acp_until_response<R: BufRead>(
    reader: &mut R,
    id: &str,
    label: &str,
) -> (Vec<String>, String) {
    let mut notifications = Vec::new();
    for _ in 0..10 {
        let mut line = String::new();
        let bytes_read = reader
            .read_line(&mut line)
            .unwrap_or_else(|err| panic!("read {label} response: {err}"));
        assert!(bytes_read > 0, "ACP server closed before {label} response");
        if line.contains(&format!("\"id\":\"{id}\"")) {
            return (notifications, line);
        }
        notifications.push(line);
    }
    panic!("ACP server did not return {label} response for id {id}");
}

fn header_value<'a>(request: &'a str, header_name: &str) -> Option<&'a str> {
    request.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case(header_name)
            .then_some(value.trim())
    })
}

fn websocket_accept_header(request: &str) -> String {
    let key = header_value(request, "sec-websocket-key").expect("websocket key header");
    let mut input = key.as_bytes().to_vec();
    input.extend_from_slice(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    let digest = ring::digest::digest(&ring::digest::SHA1_FOR_LEGACY_USE_ONLY, &input);
    BASE64_STANDARD.encode(digest.as_ref())
}

fn write_temp_config(name: &str, content: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_nanos();
    let path = workspace_target_dir().join(format!(
        "iac-code-rs-a2a-client-{name}-{}-{nanos}.yml",
        std::process::id()
    ));
    fs::write(&path, content).expect("config should be written");
    path
}

fn write_openapi_provider_config(config_dir: &Path, addr: std::net::SocketAddr) {
    fs::write(
        config_dir.join("settings.yml"),
        format!(
            "activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    apiBase: http://{addr}/v1\n    model: fixture-openapi-model\n"
        ),
    )
    .expect("settings should be written");
    fs::write(
        config_dir.join(".credentials.yml"),
        "openapi_compatible: fixture-openapi-key\n",
    )
    .expect("credentials should be written");
}

fn temp_dir(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_nanos();
    workspace_target_dir().join(format!("iac-code-rs-{name}-{}-{nanos}", std::process::id()))
}

fn workspace_target_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .expect("workspace root")
        .join("target")
}

fn short_temp_dir(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_nanos();
    PathBuf::from(format!(
        "/tmp/iac-code-rs-{name}-{}-{nanos}",
        std::process::id()
    ))
}

#[test]
fn invalid_output_format_matches_python_fixture() {
    assert_fixture("invalid_output_format");
}

#[test]
fn invalid_output_format_uses_chinese_locale_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--prompt", "hello", "--output-format", "nope"])
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "无效的 --output-format 'nope'。有效值：text, json, stream-json\n"
    );
}

#[test]
fn invalid_permission_mode_matches_python_fixture() {
    assert_fixture("invalid_permission_mode");
}

#[test]
fn invalid_permission_mode_uses_chinese_locale_like_python_typer() {
    let config_dir = temp_dir("invalid-permission-zh");
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "--prompt",
            "hello",
            "--model",
            "custom-model",
            "--permission-mode",
            "nope",
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "无效的 --permission-mode 'nope'。有效值：default, accept_edits, bypass_permissions, dont_ask\n"
    );
}

#[test]
fn invalid_provider_env_precedes_invalid_permission_mode_like_python() {
    let config_dir = temp_dir("invalid-provider-before-permission");
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args([
            "--prompt",
            "hello",
            "--output-format",
            "text",
            "--permission-mode",
            "nope",
        ])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_PROVIDER", "Nope")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid IAC_CODE_PROVIDER value: 'Nope'."),
        "{stderr}"
    );
    assert!(!stderr.contains("Invalid --permission-mode"), "{stderr}");
}

#[test]
fn invalid_provider_env_precedes_non_headless_output_format_like_python() {
    let config_dir = temp_dir("invalid-provider-before-non-headless-output-format");
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--output-format", "nope"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_PROVIDER", "Nope")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert!(
        stderr.contains("Invalid IAC_CODE_PROVIDER value: 'Nope'."),
        "{stderr}"
    );
    assert!(!stderr.contains("Invalid --output-format"), "{stderr}");
}

#[test]
fn resume_continue_conflict_matches_python_fixture() {
    assert_fixture("resume_continue_conflict");
}

#[test]
fn resume_continue_conflict_uses_chinese_locale_like_python_typer() {
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--resume", "abc", "--continue"])
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .output()
        .expect("command runs");

    assert_eq!(output.status.code(), Some(1));
    assert_eq!(String::from_utf8_lossy(&output.stdout), "");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "错误：--resume 和 --continue 不能同时使用。\n"
    );
}
