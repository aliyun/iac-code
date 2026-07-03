use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use iac_code_core::{sanitize_path, SESSION_JSONL_FILENAME, USAGE_JSONL_FILENAME};
use iac_code_tui::InputHistory;

mod common;

use common::{command_fixture, read_http_request, unique_temp_dir};

const TEST_SERVER_TIMEOUT: Duration = Duration::from_secs(20);

#[test]
fn headless_fake_cli_text_matches_python_fixture() {
    assert_fixture("text", None);
}

#[test]
fn headless_fake_cli_json_matches_python_fixture() {
    assert_fixture("json", None);
}

#[test]
fn headless_fake_cli_stream_json_matches_python_fixture() {
    assert_fixture("stream_json", None);
}

#[test]
fn headless_fake_cli_max_turns_matches_python_fixture() {
    assert_fixture("max_turns", None);
}

#[test]
fn headless_fake_cli_permission_auto_approve_matches_python_fixture() {
    let expected = command_fixture("headless_fake", "permission_auto_approve");
    let config_dir = unique_temp_dir("iac-code-rs-cli-headless-permission-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-headless-permission-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(&expected.argv)
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "write_file_auto_approve")
        .env_remove("IAC_CODE_API_KEY")
        .env_remove("IAC_CODE_PROVIDER")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    let stdout_raw =
        normalize_workspace_path(&String::from_utf8_lossy(&output.stdout), &workspace_dir);
    let stdout = strip_ansi_sequences(&stdout_raw);
    let written = fs::read_to_string(workspace_dir.join("auto-approved.txt"))
        .expect("auto-approved file should be written");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(expected.exit_code), "exit code");
    assert_eq!(stdout, expected.stdout, "stdout");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        expected.stderr,
        "stderr"
    );
    assert_eq!(written, "beta\n");
}

#[test]
fn headless_cli_debug_flag_creates_startup_log_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-headless-debug");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .args(["--debug", "--prompt", "hello"])
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .output()
        .expect("command runs");

    let log_dir = config_dir.join("logs");
    let latest_log = log_dir.join("latest.log");
    let log_files = fs::read_dir(&log_dir)
        .expect("logs dir should exist")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("log"))
        .collect::<Vec<_>>();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(latest_log.exists(), "latest.log should exist");
    assert!(
        log_files.iter().any(|path| path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("headless-"))),
        "{log_files:?}"
    );
    fs::remove_dir_all(&config_dir).ok();
}

#[test]
fn interactive_fake_cli_reads_prompt_from_stdin_and_exits() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"hello interactive\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &String::from_utf8_lossy(&output.stdout),
        &[
            "fixture response: hello interactive",
            "1 input · 2 output · 3 cache_creation · 4 cache_read",
            "✱ Processed",
        ],
    );
    assert!(
        !String::from_utf8_lossy(&output.stdout).contains("Processing"),
        "captured non-TTY output must not keep a transient working line"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_renders_assistant_markdown_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-markdown");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("listener addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"render markdown\""),
            "missing prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r###"{
                "id": "chatcmpl_markdown",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "## Background\n`iac-code` is **an IaC assistant**.\n\n| Module | Responsibility |\n| --- | --- |\n| cli | Entry point |\n\n- Generates ROS templates"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"###,
        );
    });
    write_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"render markdown\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("Background"), "{stdout}");
    assert!(stdout.contains("an IaC assistant"), "{stdout}");
    assert!(stdout.contains("Module"), "{stdout}");
    assert!(stdout.contains("Generates ROS templates"), "{stdout}");
    assert!(!stdout.contains("## Background"), "{stdout}");
    assert!(!stdout.contains("**an IaC assistant**"), "{stdout}");
    assert!(!stdout.contains("| Module | Responsibility |"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_streams_text_delta_before_response_finishes() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-streaming");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("listener addr");
    let (first_chunk_tx, first_chunk_rx) = mpsc::channel();
    let (continue_tx, continue_rx) = mpsc::channel();
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"stream please\""),
            "missing prompt in payload: {request}"
        );
        stream
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
            )
            .expect("streaming headers should be written");
        stream
            .write_all(
                br#"data: {"id":"chatcmpl_stream","choices":[{"delta":{"content":"first line\n"},"finish_reason":null}]}

"#,
            )
            .expect("first stream chunk should be written");
        stream.flush().expect("first chunk should flush");
        first_chunk_tx.send(()).expect("first chunk signal");
        continue_rx
            .recv_timeout(TEST_SERVER_TIMEOUT)
            .expect("test should allow stream to finish");
        stream
            .write_all(
                br#"data: {"id":"chatcmpl_stream","choices":[{"delta":{"content":"second line\n"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":3}}

data: [DONE]

"#,
            )
            .expect("second stream chunk should be written");
        stream.flush().expect("second chunk should flush");
    });
    write_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
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
        .expect("command starts");
    let mut stdout = child.stdout.take().expect("stdout is piped");
    let (seen_tx, seen_rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut output = String::new();
        let mut buffer = [0_u8; 64];
        let mut sent = false;
        loop {
            let read = stdout.read(&mut buffer).expect("stdout should be readable");
            if read == 0 {
                break;
            }
            output.push_str(&String::from_utf8_lossy(&buffer[..read]));
            if !sent && output.contains("first line") {
                seen_tx.send(output.clone()).ok();
                sent = true;
            }
        }
        output
    });
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"stream please\n/exit\n")
        .expect("stdin is written");

    first_chunk_rx
        .recv_timeout(TEST_SERVER_TIMEOUT)
        .expect("server should send first chunk");
    let seen_first_line = seen_rx.recv_timeout(Duration::from_secs(2));
    continue_tx.send(()).expect("allow stream to finish");
    let output = child.wait_with_output().expect("command finishes");
    let streamed_stdout = reader.join().expect("reader thread");
    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    assert!(
        seen_first_line.is_ok(),
        "first delta was not printed before the response finished; stdout was:\n{streamed_stdout}"
    );
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &streamed_stdout,
        &["first line", "second line", "✱ Processed"],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_renders_tool_progress_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-tool-progress-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-tool-progress-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("IAC_CODE_RS_FAKE_SCENARIO", "write_file_auto_approve")
        .env("LANGUAGE", "zh_CN.UTF-8")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LC_MESSAGES", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .current_dir(&workspace_dir)
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"write file\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let stdout_raw =
        normalize_workspace_path(&String::from_utf8_lossy(&output.stdout), &workspace_dir);
    let stdout = strip_ansi_sequences(&stdout_raw);
    let written = fs::read_to_string(workspace_dir.join("auto-approved.txt"))
        .expect("auto-approved file should be written");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(
        stdout_raw.contains("\x1b[32m● \x1b[0m\x1b[1m写入(auto-approved.txt)\x1b[0m"),
        "tool dot should be green while label stays bold default: {stdout_raw:?}"
    );
    assert_contains_in_order(
        &stdout,
        &[
            "● 写入(auto-approved.txt)",
            "⎿  成功写入 1 行到 $WORKSPACE/auto-approved.txt",
            "3 输入 · 4 输出",
            "permission auto approve complete",
            "5 输入 · 6 输出",
            "✱ 已处理",
        ],
    );
    assert!(
        !stdout.contains("处理中"),
        "捕获的非 TTY 输出不能留下临时工作状态：{stdout}"
    );
    assert!(
        stdout.contains("3 输入 · 4 输出") && stdout.contains("5 输入 · 6 输出"),
        "token usage should be shown: {stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert_eq!(written, "beta\n");
}

#[test]
fn interactive_fake_cli_renders_thinking_progress_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-thinking");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("listener addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"think out loud\""),
            "missing prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r###"{
                "id": "chatcmpl_thinking",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "reasoning_content": "I should inspect the repository before answering.",
                        "content": "## Result\nDone"
                    }
                }],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4}
            }"###,
        );
    });
    write_provider_config(&config_dir, addr);

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "zh_CN.UTF-8")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LC_MESSAGES", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
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
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"think out loud\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &stdout,
        &["▌ 思考完成", "Result", "9 输入 · 4 输出", "✱ 已处理"],
    );
    assert!(
        !stdout.contains("处理中"),
        "捕获的非 TTY 输出不能留下临时工作状态：{stdout}"
    );
    assert!(
        stdout.contains("9 输入 · 4 输出"),
        "token usage should be shown: {stdout}"
    );
    assert!(!stdout.contains("## Result"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_persists_prompt_input_history() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-history");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"hello history\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let history = InputHistory::new(config_dir.join(".input_history"));
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(history.entries(), vec!["hello history"]);
    assert_contains_in_order(
        &String::from_utf8_lossy(&output.stdout),
        &[
            "fixture response: hello history",
            "1 input · 2 output · 3 cache_creation · 4 cache_read",
            "✱ Processed",
        ],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_executes_shell_escape_without_provider_turn() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-shell");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"!printf shell-ok\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        stdout,
        "$ printf shell-ok\nSTDOUT:\nshell-ok\nExit code: 0\n"
    );
    assert!(
        !stdout.contains("fixture response"),
        "shell escape should not create a provider turn: {stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_empty_shell_escape_prints_usage_without_provider_turn() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-shell-empty");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"!   \n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(stdout, "Usage: !<shell command>\n");
    assert!(
        !stdout.contains("fixture response"),
        "empty shell escape should not create a provider turn: {stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_shell_escape_empty_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-shell-empty-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"!   \n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(stdout, "用法：!<shell command>\n");
    assert!(!stdout.contains("Usage: !<shell command>"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_shell_escape_denial_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-shell-denied-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--disallowed-tools")
        .arg("bash")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"!printf denied\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(stdout, "权限被拒绝。\n");
    assert!(!stdout.contains("Permission denied."), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_handles_local_slash_commands() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-commands");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/help\n/status\n/tasks\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("  └ iac-code -"), "{stdout}");
    assert!(stdout.contains("Session Status"), "{stdout}");
    assert!(!stdout.contains("  └ ╭"), "{stdout}");
    assert!(
        stderr.contains("Unknown command: /tasks. Type /help for available commands."),
        "{stderr}"
    );
    assert!(stdout.contains("Commands:"), "{stdout}");
    assert!(stdout.contains("/help"), "{stdout}");
    assert!(stdout.contains("/status"), "{stdout}");
    assert!(!stdout.contains("  /tasks"), "{stdout}");
    assert!(stdout.contains("Session Status"), "{stdout}");
    assert!(
        stdout.contains("No recorded API usage for this session yet."),
        "{stdout}"
    );
}

#[test]
fn interactive_help_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-help-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/help\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("AI 驱动的基础设施编排工具"), "{stdout}");
    assert!(stdout.contains("命令："), "{stdout}");
    assert!(stdout.contains("显示可用命令"), "{stdout}");
    assert!(stdout.contains("快捷键："), "{stdout}");
    assert!(stdout.contains("发送消息"), "{stdout}");
    assert!(!stdout.contains("Show available commands"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_status_and_memory_use_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-status-memory-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/status\n/memory\n/memory help\n/memory search missing\n/memory delete missing\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("╭"), "{stdout}");
    assert!(stdout.contains("会话状态"), "{stdout}");
    assert!(!stdout.contains("└ ╭"), "{stdout}");
    assert!(stdout.contains("│ 会话:"), "{stdout}");
    assert!(stdout.contains("│ 提供商:"), "{stdout}");
    assert!(stdout.contains("│ API Token 用量（已记录）："), "{stdout}");
    assert!(
        stdout.contains("│   此会话尚无已记录的 API 用量。"),
        "{stdout}"
    );
    assert!(!stdout.contains("│ 记忆召回"), "{stdout}");
    assert!(stdout.contains("│ 轮次:"), "{stdout}");
    assert!(stdout.contains("│ 上下文:"), "{stdout}");
    assert!(stdout.contains("╰"), "{stdout}");
    assert!(stdout.contains("记忆"), "{stdout}");
    assert!(stdout.contains("自动记忆：启用"), "{stdout}");
    assert!(stdout.contains("项目记忆"), "{stdout}");
    assert!(stdout.contains("用户记忆"), "{stdout}");
    assert!(stdout.contains("打开自动记忆文件夹"), "{stdout}");
    assert!(stdout.contains("用法：/memory-folder"), "{stdout}");
    assert!(stdout.contains("没有匹配的记忆。"), "{stdout}");
    assert!(stdout.contains("未找到记忆 'missing'。"), "{stdout}");
    assert!(
        !stdout.contains("No recorded API usage for this session yet."),
        "{stdout}"
    );
    assert!(!stdout.contains("No memories saved yet."), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_status_reports_recorded_api_usage_details() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-status-usage");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"hello status usage\n/status\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout_raw = String::from_utf8_lossy(&output.stdout);
    let stdout = strip_ansi_sequences(&stdout_raw);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(
        stdout.contains("fixture response: hello status usage"),
        "{stdout}"
    );
    assert!(stdout.contains("Session Status"), "{stdout}");
    assert!(!stdout.contains("└ ╭"), "{stdout}");
    assert!(stdout.contains("│ API Token Usage (recorded):"), "{stdout}");
    assert!(stdout.contains("│   Input:"), "{stdout}");
    assert!(stdout.contains("1"), "{stdout}");
    assert!(stdout.contains("│   Output:"), "{stdout}");
    assert!(stdout.contains("2"), "{stdout}");
    assert!(stdout.contains("│   Cache read:"), "{stdout}");
    assert!(stdout.contains("4"), "{stdout}");
    assert!(stdout.contains("│   Total:"), "{stdout}");
    assert!(stdout.contains("3"), "{stdout}");
    assert!(
        !stdout.contains("No recorded API usage for this session yet."),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_status_debug_reports_memory_recall_like_python_panel() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-status-debug-memory-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--debug")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/status\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("记忆召回"), "{stdout}");
    assert!(stdout.contains("旁路查询:"), "{stdout}");
    assert!(stdout.contains("0 次总计"), "{stdout}");
    assert!(stdout.contains("最近尝试:"), "{stdout}");
    assert!(
        stdout.contains("skipped, 耗时 0 毫秒, 选择 0 个文件"),
        "{stdout}"
    );
    assert!(stdout.contains("旁路消耗:"), "{stdout}");
    assert!(stdout.contains("最近消耗:"), "{stdout}");
    assert!(stdout.contains("未报告 token 用量"), "{stdout}");
    assert!(!stdout.contains("策略:"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_unknown_slash_command_matches_python_message() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-unknown-command");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/nosuch\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "", "stdout");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "Unknown command: /nosuch. Type /help for available commands.\n"
    );
}

#[test]
fn interactive_unknown_skill_and_command_use_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-unknown-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"$nosuch\n/nosuch\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "", "stdout");
    assert_eq!(
        stderr,
        "未知技能：$nosuch。输入 / 可列出命令和技能。\n未知命令：/nosuch。输入 /help 查看可用命令。\n"
    );
    assert!(!stderr.contains("Unknown skill"), "{stderr}");
    assert!(!stderr.contains("Unknown command"), "{stderr}");
}

#[test]
fn interactive_cli_handles_debug_command_with_session_log_path() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-debug");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-debug-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"start session\""),
            "missing initial prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_debug",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "ready"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"start session\n/debug\n/debug on\n/debug\n/debug off\n/debug invalid\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let lines = stdout.lines().collect::<Vec<_>>();
    let enabled_path = line_suffix_with_prefix(
        &lines,
        "  └ Debug logging enabled. Log file: ",
        "enabled debug path",
    );
    let status_path = line_suffix_with_prefix(
        &lines,
        "  └ Debug logging is on. Log file: ",
        "status debug path",
    );
    let enabled_path = PathBuf::from(enabled_path);
    let status_path = PathBuf::from(status_path);
    let expected_log_dir = config_dir
        .join("logs")
        .canonicalize()
        .expect("logs dir should canonicalize");

    assert_eq!(enabled_path, status_path);
    assert!(enabled_path.starts_with(expected_log_dir));
    assert_eq!(
        enabled_path.extension().and_then(|ext| ext.to_str()),
        Some("log")
    );
    assert!(enabled_path.exists(), "debug log file should exist");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &stdout,
        &[
            "ready",
            "1 input · 2 output",
            "✱ Processed",
            "  └ Debug logging is off.",
            "  └ Debug logging enabled. Log file: ",
            "  └ Debug logging is on. Log file: ",
            "  └ Debug logging disabled.",
            "  └ Usage: /debug [on|off]",
        ],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_debug_and_rename_use_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-debug-rename-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(
            b"hello\n/debug\n/debug off\n/debug invalid\n/rename\n/rename deploy-prod\n/exit\n",
        )
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("fixture response: hello"), "{stdout}");
    assert!(stdout.contains("调试日志已关闭。"), "{stdout}");
    assert!(stdout.contains("用法：/debug [on|off]"), "{stdout}");
    assert!(stdout.contains("会话名称:"), "{stdout}");
    assert!(stdout.contains("已取消重命名"), "{stdout}");
    assert!(stdout.contains("已将会话重命名为 deploy-prod"), "{stdout}");
    assert!(!stdout.contains("Debug logging is off."), "{stdout}");
    assert!(
        !stdout.contains("Renamed session to deploy-prod"),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_debug_flag_enables_startup_debug_logging_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-startup-debug");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-startup-debug-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"start session\""),
            "missing initial prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_startup_debug",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "ready"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--debug")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"start session\n/debug\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let lines = stdout.lines().collect::<Vec<_>>();
    let status_path = line_suffix_with_prefix(
        &lines,
        "  └ Debug logging is on. Log file: ",
        "debug status path",
    );
    let status_path = PathBuf::from(status_path);
    let expected_log_dir = config_dir
        .join("logs")
        .canonicalize()
        .expect("logs dir should canonicalize");

    assert!(status_path.starts_with(expected_log_dir));
    assert!(status_path.exists(), "debug log file should exist");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &stdout,
        &[
            "ready",
            "1 input · 2 output",
            "✱ Processed",
            "  └ Debug logging is on. Log file: ",
        ],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_handles_model_and_effort_commands() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-effort");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: openai\neffort: xhigh\nproviders:\n  openai:\n    effort: medium\n    model: gpt-5.4\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model\n/model gpt-5.5\n/effort\n/effort low\n/effort invalid\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ Current model: gpt-5.4\n  └ Model switched to: gpt-5.5\n  └ Current effort: medium\n  └ Effort switched to: low\n  └ Invalid effort. Allowed: low, medium, high, xhigh\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(
        saved_settings.contains("model: gpt-5.5"),
        "{saved_settings}"
    );
    assert!(saved_settings.contains("effort: xhigh"), "{saved_settings}");
    assert!(
        saved_settings.contains("openai:\n    effort: low\n    model: gpt-5.5"),
        "{saved_settings}"
    );
}

#[test]
fn interactive_model_and_effort_ignore_extra_args_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-effort-extra-args");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: openai\neffort: xhigh\nproviders:\n  openai:\n    effort: medium\n    model: gpt-5.4\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model gpt-5.5 ignored\n/effort low ignored\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ Model switched to: gpt-5.5\n  └ Effort switched to: low\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(
        saved_settings.contains("openai:\n    effort: low\n    model: gpt-5.5"),
        "{saved_settings}"
    );
}

#[test]
fn interactive_model_command_saves_valid_effort_argument() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-effort-arg");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: openai\neffort: low\nproviders:\n  openai:\n    effort: medium\n    model: gpt-5.4\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model gpt-5.5 xhigh\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ Model switched to: gpt-5.5\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(
        saved_settings.contains("openai:\n    effort: xhigh\n    model: gpt-5.5"),
        "{saved_settings}"
    );
}

#[test]
fn interactive_model_and_effort_use_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-effort-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: openai\neffort: xhigh\nproviders:\n  openai:\n    effort: medium\n    model: gpt-5.4\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model\n/model gpt-5.5\n/effort\n/effort low\n/effort invalid\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ 当前模型：gpt-5.4\n  └ 模型已切换为：gpt-5.5\n  └ 当前思考强度：medium\n  └ 思考强度已切换为：low\n  └ 非法的思考强度。允许的值：low, medium, high, xhigh\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(
        saved_settings.contains("model: gpt-5.5"),
        "{saved_settings}"
    );
    assert!(
        saved_settings.contains("openai:\n    effort: low\n    model: gpt-5.5"),
        "{saved_settings}"
    );
}

#[test]
fn interactive_model_and_effort_missing_config_use_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-effort-missing-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model gpt-5.5\n/effort\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ 没有已配置的提供商。请先运行 /auth。\n  └ 没有已配置的提供商。请先运行 /auth。\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_model_command_displays_partner_source_name_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-model-partner");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(config_dir.join("settings.yml"), "llm_source: qwenpaw\n")
        .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/model\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ Model is managed by 'QwenPaw'. To change model, modify it in QwenPaw or switch provider via /auth.\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_handles_auth_command_without_printing_secret() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-auth");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/auth\n/auth openai gpt-5.5 sk-test-secret\n/auth\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    let saved_credentials = fs::read_to_string(config_dir.join(".credentials.yml"))
        .expect("credentials should be readable");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(
        stdout.contains("No active provider configured."),
        "{stdout}"
    );
    assert!(
        stdout.contains("Authentication configured: openai / gpt-5.5"),
        "{stdout}"
    );
    assert!(stdout.contains("Active provider: openai"), "{stdout}");
    assert!(stdout.contains("Model: gpt-5.5"), "{stdout}");
    assert!(!stdout.contains("sk-test-secret"), "{stdout}");
    assert!(!stderr.contains("sk-test-secret"), "{stderr}");
    assert!(saved_settings.contains("activeProvider: openai"));
    assert!(saved_settings.contains("model: gpt-5.5"));
    assert!(saved_credentials.contains("openai: sk-test-secret"));
}

#[test]
fn interactive_login_alias_matches_auth_command() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-login");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/login openai gpt-5.5 sk-login-secret\n/auth\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    let saved_credentials = fs::read_to_string(config_dir.join(".credentials.yml"))
        .expect("credentials should be readable");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(
        stdout.contains("Authentication configured: openai / gpt-5.5"),
        "{stdout}"
    );
    assert!(stdout.contains("Active provider: openai"), "{stdout}");
    assert!(!stdout.contains("sk-login-secret"), "{stdout}");
    assert!(!stderr.contains("sk-login-secret"), "{stderr}");
    assert!(saved_settings.contains("activeProvider: openai"));
    assert!(saved_credentials.contains("openai: sk-login-secret"));
}

#[test]
fn interactive_fake_cli_handles_memory_command() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-memory");
    let memory_dir = config_dir.join("memory");
    fs::create_dir_all(&memory_dir).expect("memory dir should be created");
    fs::write(
        memory_dir.join("project-note.md"),
        "---\nname: project-note\ndescription: Testing rule\ntype: project\n---\n\nUse fake providers in tests.\n",
    )
    .expect("memory file should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(
            b"/memory\n/memory project-note\n/memory search fake providers\n/memory delete project-note\n/memory project-note\n/memory help\n/exit\n",
        )
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Memory"), "{stdout}");
    assert!(stdout.contains("Auto-memory: on"), "{stdout}");
    assert!(stdout.contains("Project memory"), "{stdout}");
    assert!(stdout.contains("User memory"), "{stdout}");
    assert!(stdout.contains("Open auto-memory folder"), "{stdout}");
    assert!(
        stdout.contains("[project] Testing rule\n\nUse fake providers in tests."),
        "{stdout}"
    );
    assert!(
        stdout.contains("Matching memories:\n  - project-note - Testing rule"),
        "{stdout}"
    );
    assert!(
        stdout.contains("Memory 'project-note' deleted."),
        "{stdout}"
    );
    assert!(
        stdout.contains("Memory 'project-note' not found."),
        "{stdout}"
    );
    assert!(
        stdout.contains("Usage: /memory-folder [<name>|search <query>|delete <name>|help]"),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_fake_cli_handles_skills_command() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-skills-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-skills-workspace");
    let user_skill_dir = config_dir.join("skills").join("user-helper");
    let project_skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("project-helper");
    fs::create_dir_all(&user_skill_dir).expect("user skill dir should be created");
    fs::create_dir_all(&project_skill_dir).expect("project skill dir should be created");
    fs::write(
        user_skill_dir.join("SKILL.md"),
        "---\ndescription: User helper\n---\n\nUser instructions.\n",
    )
    .expect("user skill should be written");
    fs::write(
        project_skill_dir.join("SKILL.md"),
        "---\ndescription: Project helper\n---\n\nProject instructions.\n",
    )
    .expect("project skill should be written");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: dashscope\ndisabled_skills:\n- user-helper\nproviders:\n  dashscope:\n    model: saved-model\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(
            b"/skills\n/skills disable project-helper\n/skills enable user-helper\n/skills disable iac-aliyun\n/skills help\n/exit\n",
        )
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    let saved_settings =
        fs::read_to_string(config_dir.join("settings.yml")).expect("settings should be readable");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(
        stdout.contains("iac-aliyun [enabled, locked, bundled]"),
        "{stdout}"
    );
    assert!(
        stdout.contains("project-helper [enabled, project] - Project helper"),
        "{stdout}"
    );
    assert!(
        stdout.contains("user-helper [disabled, user] - User helper"),
        "{stdout}"
    );
    assert!(
        stdout.contains("Skill 'project-helper' disabled."),
        "{stdout}"
    );
    assert!(stdout.contains("Skill 'user-helper' enabled."), "{stdout}");
    assert!(
        stdout.contains("Skill 'iac-aliyun' is bundled and cannot be disabled."),
        "{stdout}"
    );
    assert!(
        stdout.contains("Usage: /skills [list|enable <name>|disable <name>|help]"),
        "{stdout}"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(saved_settings.contains("activeProvider: dashscope"));
    assert!(
        saved_settings.contains("providers:\n  dashscope:\n    model: saved-model"),
        "{saved_settings}"
    );
    assert!(saved_settings.contains("disabled_skills:\n- project-helper"));
    assert!(!saved_settings.contains("- user-helper"));
    assert!(!saved_settings.contains("- iac-aliyun"));
}

#[test]
fn interactive_skills_uses_chinese_locale_like_python_picker() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-skills-zh-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-skills-zh-workspace");
    let user_skill_dir = config_dir.join("skills").join("user-helper");
    let project_skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("project-helper");
    fs::create_dir_all(&user_skill_dir).expect("user skill dir should be created");
    fs::create_dir_all(&project_skill_dir).expect("project skill dir should be created");
    fs::write(
        user_skill_dir.join("SKILL.md"),
        "---\ndescription: User helper\n---\n\nUser instructions.\n",
    )
    .expect("user skill should be written");
    fs::write(
        project_skill_dir.join("SKILL.md"),
        "---\ndescription: Project helper\n---\n\nProject instructions.\n",
    )
    .expect("project skill should be written");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: dashscope\ndisabled_skills:\n- user-helper\nproviders:\n  dashscope:\n    model: saved-model\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/skills\n/skills disable project-helper\n/skills disable iac-aliyun\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("技能:"), "{stdout}");
    assert!(
        stdout.contains("iac-aliyun [启用, 已锁定, 内置]"),
        "{stdout}"
    );
    assert!(
        stdout.contains("project-helper [启用, 项目] - Project helper"),
        "{stdout}"
    );
    assert!(
        stdout.contains("user-helper [禁用, 用户] - User helper"),
        "{stdout}"
    );
    assert!(stdout.contains("技能已禁用：project-helper"), "{stdout}");
    assert!(
        stdout.contains("iac-aliyun: 内置技能不能被禁用。"),
        "{stdout}"
    );
    assert!(!stdout.contains("Skills:"), "{stdout}");
    assert!(!stdout.contains("[enabled, locked, bundled]"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_hides_disabled_skills_from_provider_prompt() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-disabled-skills-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-disabled-skills-workspace");
    let enabled_skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("enabled-helper");
    let disabled_skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("disabled-helper");
    fs::create_dir_all(&enabled_skill_dir).expect("enabled skill dir should be created");
    fs::create_dir_all(&disabled_skill_dir).expect("disabled skill dir should be created");
    fs::write(
        enabled_skill_dir.join("SKILL.md"),
        "---\ndescription: Enabled helper\n---\n\nEnabled instructions.\n",
    )
    .expect("enabled skill should be written");
    fs::write(
        disabled_skill_dir.join("SKILL.md"),
        "---\ndescription: Disabled helper\n---\n\nDisabled instructions.\n",
    )
    .expect("disabled skill should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("enabled-helper"),
            "enabled skill missing from provider payload: {request}"
        );
        assert!(
            !request.contains("disabled-helper"),
            "disabled skill leaked into provider payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_disabled_skills",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "ready"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        format!(
            "activeProvider: openapi_compatible\ndisabled_skills:\n- disabled-helper\nproviders:\n  openapi_compatible:\n    apiBase: http://{addr}/v1\n    model: fixture-openapi-model\n"
        ),
    )
    .expect("settings should be written");
    fs::write(
        config_dir.join(".credentials.yml"),
        "openapi_compatible: fixture-openapi-key\n",
    )
    .expect("credentials should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "ready\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_invokes_user_skill_with_dollar_trigger() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-skill-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-skill-workspace");
    let skill_dir = workspace_dir.join(".iac-code").join("skills").join("demo");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&skill_dir).expect("skill dir should be created");
    fs::write(
        skill_dir.join("SKILL.md"),
        "---\ndescription: Demo helper\n---\n\nRender skill args: $ARGUMENTS\n",
    )
    .expect("skill should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("<skill-name>demo</skill-name>"),
            "skill tag missing from provider payload: {request}"
        );
        assert!(
            request.contains("Render skill args: prod stack"),
            "rendered skill arguments missing from provider payload: {request}"
        );
        assert!(
            !request.contains("$demo prod stack"),
            "raw skill command leaked into provider payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_interactive_skill",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "skill invoked"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"$demo prod stack\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    let history = InputHistory::new(config_dir.join(".input_history"));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(history.entries(), vec!["$demo prod stack"]);
    assert_contains_in_order(
        &String::from_utf8_lossy(&output.stdout),
        &["skill invoked", "1 input · 2 output", "✱ Processed"],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_disabled_skill_error_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-disabled-skill-zh-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-disabled-skill-zh-workspace");
    let skill_dir = workspace_dir.join(".iac-code").join("skills").join("demo");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&skill_dir).expect("skill dir should be created");
    fs::write(
        skill_dir.join("SKILL.md"),
        "---\ndescription: Demo helper\n---\n\nRender skill args: $ARGUMENTS\n",
    )
    .expect("skill should be written");
    fs::write(
        config_dir.join("settings.yml"),
        "disabled_skills:\n- demo\n",
    )
    .expect("settings should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"$demo prod stack\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "", "stdout");
    assert_eq!(stderr, "技能“demo”已禁用。运行 /skills 以启用它。\n");
    assert!(!stderr.contains("Skill 'demo' is disabled"), "{stderr}");
}

#[test]
fn interactive_cli_invokes_fork_skill_as_rendered_prompt() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-fork-skill-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-fork-skill-workspace");
    let skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("fork-demo");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&skill_dir).expect("skill dir should be created");
    fs::write(
        skill_dir.join("SKILL.md"),
        "---\ndescription: Fork helper\ncontext: fork\nagent: explore\n---\n\nInvestigate $ARGUMENTS\n",
    )
    .expect("skill should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("Investigate prod stack"),
            "rendered fork prompt missing from provider payload: {request}"
        );
        assert!(
            !request.contains("<skill-name>fork-demo</skill-name>"),
            "fork prompt should not be tagged as an inline skill: {request}"
        );
        assert!(
            !request.contains("$fork-demo prod stack"),
            "raw fork skill command leaked into provider payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_interactive_fork_skill",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "fork skill invoked"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"$fork-demo prod stack\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &String::from_utf8_lossy(&output.stdout),
        &["fork skill invoked", "1 input · 2 output", "✱ Processed"],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_clear_resets_session_history() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-clear-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-clear-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"content\":\"first interactive\""),
            "missing first prompt in payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_interactive_clear_1",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "first answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );

        let (mut second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"content\":\"second interactive\""),
            "missing second prompt in payload: {second_request}"
        );
        assert!(
            !second_request.contains("\"content\":\"first interactive\""),
            "clear did not remove first user message from second payload: {second_request}"
        );
        assert!(
            !second_request.contains("\"content\":\"first answer\""),
            "clear did not remove first assistant message from second payload: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_interactive_clear_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "second answer"}
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"first interactive\n/clear\nsecond interactive\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_contains_in_order(
        &String::from_utf8_lossy(&output.stdout),
        &[
            "first answer",
            "1 input · 2 output",
            "✱ Processed",
            "second answer",
            "3 input · 4 output",
            "✱ Processed",
        ],
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_handles_rename_and_resume_commands() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-rename-resume-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-rename-resume-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"content\":\"first prompt\""),
            "missing first prompt in payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_interactive_rename_resume_1",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "first answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );

        let (mut second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"content\":\"first prompt\""),
            "resume did not include first prompt in second payload: {second_request}"
        );
        assert!(
            second_request.contains("\"content\":\"second prompt\""),
            "missing second prompt in payload: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_interactive_rename_resume_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "second answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"first prompt\n/rename deploy-prod\n/resume\n/resume deploy-prod\nsecond prompt\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    let session_dirs = session_dirs_for(&config_dir, &workspace_dir);
    let session_raw = session_dirs
        .iter()
        .find_map(|dir| fs::read_to_string(dir.join(SESSION_JSONL_FILENAME)).ok())
        .expect("session jsonl should be readable");
    let metadata_raw = session_dirs
        .iter()
        .find_map(|dir| fs::read_to_string(dir.join("metadata.json")).ok())
        .expect("metadata should be readable");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("first answer"), "{stdout}");
    assert!(
        stdout.contains("Renamed session to deploy-prod")
            || stdout.contains("已将会话重命名为 deploy-prod"),
        "{stdout}"
    );
    assert!(stdout.contains("Sessions:"), "{stdout}");
    assert!(stdout.contains("deploy-prod"), "{stdout}");
    assert!(stdout.contains("Resuming session: deploy-prod"), "{stdout}");
    assert!(stdout.contains("second answer"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(session_raw.contains("\"content\":\"first prompt\""));
    assert!(session_raw.contains("\"content\":\"second prompt\""));
    assert!(metadata_raw.contains("\"name\":\"deploy-prod\""));
}

#[test]
fn interactive_resume_not_found_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-resume-not-found-zh");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-resume-not-found-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/resume missing-session\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "  └ 未找到会话：missing-session\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn interactive_cli_compact_rewrites_session_history_for_next_prompt() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-compact-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-interactive-compact-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        for index in 1..=4 {
            let (mut stream, _) =
                accept_with_timeout(listener.try_clone().expect("clone listener"));
            configure_test_stream(&stream);
            let request = read_http_request(&mut stream);
            assert!(
                request.contains(&format!("old prompt {index}")),
                "missing old prompt {index} in payload: {request}"
            );
            write_http_response(
                &mut stream,
                &format!(
                    r#"{{
                        "id": "chatcmpl_interactive_compact_{index}",
                        "choices": [{{
                            "finish_reason": "stop",
                            "message": {{"content": "old answer {index}"}}
                        }}],
                        "usage": {{"prompt_tokens": 1, "completion_tokens": 2}}
                    }}"#
                ),
            );
        }

        let (mut compact_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&compact_stream);
        let compact_request = read_http_request(&mut compact_stream);
        assert!(
            compact_request.contains("Please provide a concise summary"),
            "missing compaction prompt: {compact_request}"
        );
        assert!(
            compact_request.contains("USER: old prompt 1")
                && compact_request.contains("ASSISTANT: old answer 1"),
            "compaction prompt did not include old turn: {compact_request}"
        );
        assert!(
            !compact_request.contains("USER: old prompt 2"),
            "compaction prompt should preserve recent turns outside summary input: {compact_request}"
        );
        write_http_response(
            &mut compact_stream,
            r#"{
                "id": "chatcmpl_interactive_compact_summary",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "summary of compressed history"}
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut final_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&final_stream);
        let final_request = read_http_request(&mut final_stream);
        assert!(
            final_request.contains("[Conversation Summary]")
                && final_request.contains("summary of compressed history"),
            "next prompt did not include compacted summary: {final_request}"
        );
        assert!(
            final_request.contains("\"content\":\"old prompt 2\"")
                && final_request.contains("\"content\":\"old answer 4\""),
            "next prompt did not preserve recent messages: {final_request}"
        );
        assert!(
            !final_request.contains("\"content\":\"old prompt 1 ")
                && !final_request.contains("\"content\":\"old answer 1\""),
            "next prompt still included compacted-away messages: {final_request}"
        );
        assert!(
            final_request.contains("\"content\":\"after compact\""),
            "missing final prompt in payload: {final_request}"
        );
        write_http_response(
            &mut final_stream,
            r#"{
                "id": "chatcmpl_interactive_compact_final",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "after answer"}
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "en")
        .env("LC_ALL", "en_US.UTF-8")
        .env("LC_MESSAGES", "en_US.UTF-8")
        .env("LANG", "en_US.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    let prompt_one = format!("old prompt 1 {}", "alpha ".repeat(16_000));
    let input = format!(
        "{prompt_one}\nold prompt 2\nold prompt 3\nold prompt 4\n/compact\nafter compact\n/exit\n"
    );
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(input.as_bytes())
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    server.join().expect("server thread");
    let session_dirs = session_dirs_for(&config_dir, &workspace_dir);
    let session_raw = session_dirs
        .iter()
        .find_map(|dir| fs::read_to_string(dir.join(SESSION_JSONL_FILENAME)).ok())
        .expect("session jsonl should be readable");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert!(stdout.contains("Context compacted:"), "{stdout}");
    assert!(stdout.contains("after answer"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
    assert!(session_raw.contains("[Conversation Summary]"));
    assert!(session_raw.contains("summary of compressed history"));
    assert!(!session_raw.contains("\"content\":\"old prompt 1 "));
    assert!(!session_raw.contains("\"content\":\"old answer 1\""));
    assert!(session_raw.contains("\"content\":\"old prompt 2\""));
    assert!(session_raw.contains("\"content\":\"after compact\""));
}

#[test]
fn interactive_compact_empty_uses_chinese_locale_like_python_repl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-interactive-compact-empty-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("command starts");
    child
        .stdin
        .take()
        .expect("stdin is piped")
        .write_all(b"/compact\n/exit\n")
        .expect("stdin is written");
    let output = child.wait_with_output().expect("command finishes");
    fs::remove_dir_all(&config_dir).ok();

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(stdout, "  └ 无内容可压缩：对话为空。\n");
    assert!(!stdout.contains("Nothing to compact"), "{stdout}");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_uses_saved_openapi_compatible_provider_config() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-provider");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        stream
            .set_nonblocking(false)
            .expect("accepted stream should be blocking");
        stream
            .set_read_timeout(Some(TEST_SERVER_TIMEOUT))
            .expect("accepted stream should have read timeout");
        let request = read_http_request(&mut stream);
        assert!(
            request.starts_with("POST /v1/chat/completions HTTP/1.1"),
            "unexpected request line: {request}"
        );
        assert!(
            request.contains("authorization: Bearer fixture-openapi-key")
                || request.contains("Authorization: Bearer fixture-openapi-key"),
            "missing bearer auth header: {request}"
        );
        assert!(
            request.contains("\"model\":\"fixture-openapi-model\""),
            "missing configured model in payload: {request}"
        );
        assert!(
            request.contains("\"role\":\"system\""),
            "missing system message in payload: {request}"
        );
        assert!(
            request.contains(
                "You are an expert AI coding assistant specialized in Infrastructure as Code"
            ),
            "missing system prompt identity in payload: {request}"
        );
        assert!(
            request.contains("# Memory") && request.contains("[project] Prefer mock providers."),
            "missing memory prompt content in payload: {request}"
        );
        assert!(
            request.contains("# Available Skills") && request.contains("iac-aliyun"),
            "missing skill listing in payload: {request}"
        );
        assert!(
            request.contains("\"content\":\"hello\""),
            "missing prompt in payload: {request}"
        );
        assert!(
            request.contains("\"tools\":["),
            "missing tool definitions in payload: {request}"
        );
        for tool_name in [
            "read_file",
            "bash",
            "read_memory",
            "task_list",
            "skill",
            "agent",
        ] {
            assert!(
                request.contains(&format!("\"name\":\"{tool_name}\"")),
                "missing {tool_name} tool definition in payload: {request}"
            );
        }
        let body = r#"{
            "id": "chatcmpl_cli",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "mocked"}
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

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
    fs::create_dir_all(config_dir.join("memory")).expect("memory dir should be created");
    fs::write(
        config_dir.join("memory").join("project-note.md"),
        "---\nname: project-note\ndescription: Testing\ntype: project\n---\n\nPrefer mock providers.\n",
    )
    .expect("memory file should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "mocked\n",
        "stdout"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_uses_anthropic_compatible_provider_protocol() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-anthropic-provider");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.starts_with("POST /v1/messages HTTP/1.1"),
            "unexpected request line: {request}"
        );
        assert!(
            request.contains("x-api-key: fixture-anthropic-key")
                || request.contains("X-Api-Key: fixture-anthropic-key"),
            "missing anthropic api key header: {request}"
        );
        assert!(
            request.contains("anthropic-version: 2023-06-01"),
            "missing anthropic version header: {request}"
        );
        assert!(
            request.contains("\"model\":\"fixture-claude-model\""),
            "missing configured model in payload: {request}"
        );
        assert!(
            request.contains("\"system\":\"You are an expert AI coding assistant"),
            "missing anthropic system field in payload: {request}"
        );
        assert!(
            request.contains("\"role\":\"user\"")
                && request.contains("\"content\":\"hello anthropic\""),
            "missing user prompt in anthropic messages payload: {request}"
        );
        assert!(
            request.contains("\"input_schema\"") && !request.contains("\"function\""),
            "tools should use anthropic schema shape: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "msg_cli_anthropic",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "anthropic mocked"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2}
            }"#,
        );
    });

    fs::write(
        config_dir.join("settings.yml"),
        format!(
            "activeProvider: anthropic_compatible\nproviders:\n  anthropic_compatible:\n    apiBase: http://{addr}\n    model: fixture-claude-model\n"
        ),
    )
    .expect("settings should be written");
    fs::write(
        config_dir.join(".credentials.yml"),
        "anthropic_compatible: fixture-anthropic-key\n",
    )
    .expect("credentials should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello anthropic")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "anthropic mocked\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_auto_triggers_iac_aliyun_skill_before_provider_prompt() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-auto-trigger-config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        let skill_index = request
            .find("<skill-name>iac-aliyun</skill-name>")
            .unwrap_or_else(|| panic!("missing auto-triggered iac-aliyun message: {request}"));
        let prompt_index = request
            .find("rust-auto-trigger-marker")
            .unwrap_or_else(|| panic!("missing original prompt in payload: {request}"));
        assert!(
            skill_index < prompt_index,
            "auto-triggered skill must be injected before original prompt: {request}"
        );
        assert!(
            request.contains("Base directory for this skill:"),
            "missing rendered skill prompt content: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_auto_trigger",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "auto loaded"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("Generate an Alibaba Cloud ROS template for rust-auto-trigger-marker")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "auto loaded\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_persists_new_session_jsonl() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-session-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-session-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"fresh prompt\""),
            "missing fresh prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_session_new",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "persisted answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("fresh prompt")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "persisted answer\n"
    );

    let sessions = session_dirs_for(&config_dir, &workspace_dir);
    assert_eq!(sessions.len(), 1, "expected one persisted session");
    let raw = fs::read_to_string(sessions[0].join(SESSION_JSONL_FILENAME))
        .expect("session jsonl should be readable");
    assert!(raw.contains("\"role\":\"user\""));
    assert!(raw.contains("\"content\":\"fresh prompt\""));
    assert!(raw.contains("\"cwd\":\""));
    assert!(raw.contains(&workspace_cwd(&workspace_dir)));
    assert!(raw.contains("persisted answer"));
    let usage_raw = fs::read_to_string(sessions[0].join(USAGE_JSONL_FILENAME))
        .expect("usage sidecar should be readable");
    assert!(usage_raw.contains(r#""type":"usage""#), "{usage_raw}");
    assert!(usage_raw.contains(r#""input_tokens":1"#), "{usage_raw}");
    assert!(usage_raw.contains(r#""output_tokens":2"#), "{usage_raw}");
    assert!(
        usage_raw.contains(r#""provider":"openapi_compatible""#),
        "{usage_raw}"
    );
    assert!(
        usage_raw.contains(r#""model":"fixture-openapi-model""#),
        "{usage_raw}"
    );

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn headless_cli_resume_loads_previous_messages_and_appends_to_same_session() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-resume-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-resume-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let session_dir = config_dir
        .join("projects")
        .join(sanitize_path(&workspace_cwd(&workspace_dir)))
        .join("resume-id");
    fs::create_dir_all(&session_dir).expect("session dir should be created");
    fs::write(
        session_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"old prompt\",\"session_id\":\"resume-id\",\"cwd\":\"{}\",\"version\":\"0.4.0\"}}\n{{\"role\":\"assistant\",\"content\":\"old answer\",\"session_id\":\"resume-id\",\"cwd\":\"{}\",\"version\":\"0.4.0\"}}\n",
            workspace_cwd(&workspace_dir),
            workspace_cwd(&workspace_dir)
        ),
    )
    .expect("existing session should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"old prompt\""),
            "missing resumed user message in payload: {request}"
        );
        assert!(
            request.contains("\"content\":\"old answer\""),
            "missing resumed assistant message in payload: {request}"
        );
        assert!(
            request.contains("\"content\":\"next prompt\""),
            "missing next prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_session_resume",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "resumed answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("next prompt")
        .arg("--resume")
        .arg("resume-id")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "resumed answer\n");
    let raw = fs::read_to_string(session_dir.join(SESSION_JSONL_FILENAME))
        .expect("session jsonl should be readable");
    assert!(raw.contains("\"content\":\"old prompt\""));
    assert!(raw.contains("\"content\":\"next prompt\""));
    assert!(raw.contains("resumed answer"));

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn headless_cli_continue_loads_latest_session_and_appends() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-continue-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-continue-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let project_dir = config_dir
        .join("projects")
        .join(sanitize_path(&workspace_cwd(&workspace_dir)));
    let older_dir = project_dir.join("older-id");
    let latest_dir = project_dir.join("latest-id");
    fs::create_dir_all(&older_dir).expect("older session dir should be created");
    fs::write(
        older_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"older prompt\",\"session_id\":\"older-id\",\"cwd\":\"{}\",\"version\":\"0.4.0\"}}\n",
            workspace_cwd(&workspace_dir)
        ),
    )
    .expect("older session should be written");
    thread::sleep(Duration::from_millis(20));
    fs::create_dir_all(&latest_dir).expect("latest session dir should be created");
    fs::write(
        latest_dir.join(SESSION_JSONL_FILENAME),
        format!(
            "{{\"role\":\"user\",\"content\":\"latest prompt\",\"session_id\":\"latest-id\",\"cwd\":\"{}\",\"version\":\"0.4.0\"}}\n",
            workspace_cwd(&workspace_dir)
        ),
    )
    .expect("latest session should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"content\":\"latest prompt\""),
            "missing latest session history in payload: {request}"
        );
        assert!(
            request.contains("\"content\":\"continue prompt\""),
            "missing continue prompt in payload: {request}"
        );
        assert!(
            !request.contains("\"content\":\"older prompt\""),
            "older session was unexpectedly resumed: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_session_continue",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "continued answer"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("continue prompt")
        .arg("--continue")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "continued answer\n"
    );
    let raw = fs::read_to_string(latest_dir.join(SESSION_JSONL_FILENAME))
        .expect("latest session jsonl should be readable");
    assert!(raw.contains("\"content\":\"latest prompt\""));
    assert!(raw.contains("\"content\":\"continue prompt\""));
    assert!(raw.contains("continued answer"));

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();
}

#[test]
fn headless_cli_model_option_overrides_saved_provider_model() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-model-override");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"model\":\"cli-openapi-model\""),
            "missing CLI model override in payload: {request}"
        );
        assert!(
            !request.contains("\"model\":\"saved-openapi-model\""),
            "saved model was not overridden: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_cli_model",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "model override"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    fs::write(
        config_dir.join("settings.yml"),
        format!(
            "activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    apiBase: http://{addr}/v1\n    model: saved-openapi-model\n"
        ),
    )
    .expect("settings should be written");
    fs::write(
        config_dir.join(".credentials.yml"),
        "openapi_compatible: fixture-openapi-key\n",
    )
    .expect("credentials should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .arg("--model")
        .arg("cli-openapi-model")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "model override\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_reads_qwenpaw_provider_config_when_selected() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-config");
    let qwenpaw_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-secret");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(qwenpaw_dir.join("providers").join("custom"))
        .expect("qwenpaw providers dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.starts_with("POST /v1/chat/completions HTTP/1.1"),
            "unexpected request line: {request}"
        );
        assert!(
            request.contains("authorization: Bearer fixture-qwenpaw-key")
                || request.contains("Authorization: Bearer fixture-qwenpaw-key"),
            "missing qwenpaw bearer auth header: {request}"
        );
        assert!(
            request.contains("\"model\":\"fixture-qwenpaw-model\""),
            "missing qwenpaw model in payload: {request}"
        );
        assert!(
            request.contains("\"content\":\"hello from qwenpaw\""),
            "missing prompt in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_qwenpaw",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "qwenpaw mocked"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    fs::write(config_dir.join("settings.yml"), "llm_source: qwenpaw\n")
        .expect("settings should be written");
    fs::write(config_dir.join(".credentials.yml"), "").expect("credentials should be written");
    fs::write(
        qwenpaw_dir.join("providers").join("active_model.json"),
        r#"{"model":"fixture-qwenpaw-model","provider_id":"openai"}"#,
    )
    .expect("active model should be written");
    fs::write(
        qwenpaw_dir
            .join("providers")
            .join("custom")
            .join("openai.json"),
        format!(r#"{{"api_key":"fixture-qwenpaw-key","base_url":"http://{addr}/v1"}}"#),
    )
    .expect("provider config should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello from qwenpaw")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("QWENPAW_SECRET_DIR", &qwenpaw_dir)
        .env_remove("COPAW_SECRET_DIR")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&qwenpaw_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "qwenpaw mocked\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_missing_provider_key_uses_chinese_locale_like_python_cli() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-missing-provider-key-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .arg("--model")
        .arg("gpt-5.5")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(1), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "", "stdout");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "提供商 'OpenAI' 未配置 API 密钥（模型: gpt-5.5）。请运行 /auth 进行配置。\n"
    );
}

#[test]
fn headless_unknown_model_provider_uses_chinese_locale_like_python_cli() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-unknown-model-provider-zh");
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .arg("--model")
        .arg("custom-local-model")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("LANGUAGE", "zh")
        .env("LC_ALL", "zh_CN.UTF-8")
        .env("LANG", "zh_CN.UTF-8")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");
    fs::remove_dir_all(&config_dir).ok();

    assert_eq!(output.status.code(), Some(1), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "", "stdout");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "无法确定模型 custom-local-model 的提供商。请运行 /auth 进行配置。\n"
    );
}

#[test]
fn headless_cli_decrypts_qwenpaw_encrypted_api_key_from_master_key_file() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-enc-config");
    let qwenpaw_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-enc-secret");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(qwenpaw_dir.join("providers").join("custom"))
        .expect("qwenpaw providers dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("authorization: Bearer hello fernet")
                || request.contains("Authorization: Bearer hello fernet"),
            "missing decrypted qwenpaw bearer auth header: {request}"
        );
        assert!(
            request.contains("\"model\":\"fixture-qwenpaw-encrypted-model\""),
            "missing qwenpaw model in payload: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_qwenpaw_encrypted",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "qwenpaw encrypted mocked"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }"#,
        );
    });

    fs::write(config_dir.join("settings.yml"), "llm_source: qwenpaw\n")
        .expect("settings should be written");
    fs::write(config_dir.join(".credentials.yml"), "").expect("credentials should be written");
    fs::write(
        qwenpaw_dir.join(".master_key"),
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f\n",
    )
    .expect("master key should be written");
    fs::write(
        qwenpaw_dir.join("providers").join("active_model.json"),
        r#"{"model":"fixture-qwenpaw-encrypted-model","provider_id":"openai"}"#,
    )
    .expect("active model should be written");
    fs::write(
        qwenpaw_dir
            .join("providers")
            .join("custom")
            .join("openai.json"),
        format!(
            r#"{{"api_key":"ENC:gAAAAABlU_EAEBESExQVFhcYGRobHB0eH-PL8hlOsFk83vaJHIwd73emw-xQHoM-bLNpYv_5oKQU2zutDFYIUMNJZVhc2tZN-w==","base_url":"http://{addr}/v1"}}"#
        ),
    )
    .expect("provider config should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello from encrypted qwenpaw")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("QWENPAW_SECRET_DIR", &qwenpaw_dir)
        .env_remove("COPAW_SECRET_DIR")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&qwenpaw_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "qwenpaw encrypted mocked\n"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_reports_qwenpaw_unknown_provider_with_supported_ids() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-unknown-config");
    let qwenpaw_dir = unique_temp_dir("iac-code-rs-cli-qwenpaw-unknown-secret");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(qwenpaw_dir.join("providers"))
        .expect("qwenpaw providers dir should be created");
    fs::write(config_dir.join("settings.yml"), "llm_source: qwenpaw\n")
        .expect("settings should be written");
    fs::write(
        qwenpaw_dir.join("providers").join("active_model.json"),
        r#"{"model":"fixture-model","provider_id":"unknown-provider"}"#,
    )
    .expect("active model should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("hello")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env("QWENPAW_SECRET_DIR", &qwenpaw_dir)
        .env_remove("COPAW_SECRET_DIR")
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .output()
        .expect("command runs");

    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&qwenpaw_dir).ok();

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(1), "exit code");
    assert!(
        stderr.contains("Unknown provider 'unknown-provider'"),
        "{stderr}"
    );
    assert!(
        stderr.contains("Supported QwenPaw provider IDs:"),
        "{stderr}"
    );
    assert!(stderr.contains("openai"), "{stderr}");
    assert!(stderr.contains("aliyun-codingplan"), "{stderr}");
    assert!(stderr.contains("disable QwenPaw mode"), "{stderr}");
}

#[test]
fn headless_cli_executes_provider_tool_call_and_continues() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-tool-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-tool-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    fs::write(workspace_dir.join("status.txt"), "alpha\n")
        .expect("workspace file should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"name\":\"read_file\""),
            "missing read_file tool definition in first payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_tool_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{\"path\":\"status.txt\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"tool_call_id\":\"call_read\""),
            "missing tool result call id in second payload: {second_request}"
        );
        assert!(
            second_request.contains("alpha"),
            "missing read_file result content in second payload: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_tool_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "after tool"}
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );
    });

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

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("read status")
        .arg("--verbose")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "after tool\n",
        "stdout"
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "Tool started: read_file\nTool finished: read_file\n",
        "stderr"
    );
}

#[test]
fn headless_cli_registers_aliyun_doc_search_when_cloud_credentials_exist() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-aliyun-doc-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-aliyun-doc-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("\"name\":\"aliyun_doc_search\""),
            "missing aliyun_doc_search tool definition: {request}"
        );
        assert!(
            request.contains("\"name\":\"aliyun_api\""),
            "missing aliyun_api tool definition: {request}"
        );
        assert!(
            request.contains("\"name\":\"ros_stack\""),
            "missing ros_stack tool definition: {request}"
        );
        assert!(
            request.contains("\"name\":\"ros_stack_instances\""),
            "missing ros_stack_instances tool definition: {request}"
        );
        assert!(
            !request.contains("fixture-cloud-secret"),
            "cloud secret leaked into provider request: {request}"
        );
        write_http_response(
            &mut stream,
            r#"{
                "id": "chatcmpl_aliyun_doc",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "ready"}
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    fs::write(
        config_dir.join(".cloud-credentials.yml"),
        "aliyun:\n  mode: AK\n  access_key_id: fixture-cloud-ak\n  access_key_secret: fixture-cloud-secret\n  region_id: cn-hangzhou\n",
    )
    .expect("cloud credentials should be written");

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("search ros docs")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "ready\n");
}

#[test]
fn headless_cli_skill_allowed_tools_apply_to_later_tool_calls() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-skill-allowed-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-skill-allowed-workspace");
    let skill_dir = workspace_dir
        .join(".iac-code")
        .join("skills")
        .join("write-helper");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&skill_dir).expect("skill dir should be created");
    fs::write(
        skill_dir.join("SKILL.md"),
        "---\ndescription: Write helper\nallowed_tools:\n  - write_file\n---\n\nUse write_file.\n",
    )
    .expect("skill should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"name\":\"skill\""),
            "missing skill tool definition in first payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_skill_allowed_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_skill",
                            "type": "function",
                            "function": {
                                "name": "skill",
                                "arguments": "{\"skill\":\"write-helper\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut second_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("Skill 'write-helper' loaded (inline)."),
            "missing skill tool result in second payload: {second_request}"
        );
        assert!(
            second_request.contains("<skill-name>write-helper</skill-name>"),
            "missing injected skill message in second payload: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_skill_allowed_2",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"allowed-created.txt\",\"content\":\"allowed\\n\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );

        let (mut third_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&third_stream);
        let third_request = read_http_request(&mut third_stream);
        assert!(
            third_request.contains("\"tool_call_id\":\"call_write\""),
            "missing write_file result in third payload: {third_request}"
        );
        assert!(
            third_request.contains("Successfully wrote 1 lines"),
            "skill allowed_tools should allow write_file under dont_ask: {third_request}"
        );
        assert!(
            !third_request.contains("Permission denied."),
            "write_file was denied despite skill allowed_tools: {third_request}"
        );
        write_http_response(
            &mut third_stream,
            r#"{
                "id": "chatcmpl_skill_allowed_3",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "done"}
                }],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8}
            }"#,
        );
    });

    write_provider_config(&config_dir, addr);
    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("use skill allowed tools")
        .arg("--permission-mode")
        .arg("dont_ask")
        .arg("--allowed-tools")
        .arg("skill")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    assert_eq!(
        fs::read_to_string(workspace_dir.join("allowed-created.txt"))
            .expect("allowed file should be written"),
        "allowed\n"
    );
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "done\n", "stdout");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_agent_tool_runs_child_with_default_tools() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-agent-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-agent-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    fs::write(workspace_dir.join("status.txt"), "alpha child\n")
        .expect("workspace file should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut parent_first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&parent_first_stream);
        let parent_first_request = read_http_request(&mut parent_first_stream);
        assert!(
            parent_first_request.contains("\"name\":\"agent\""),
            "missing agent tool definition in parent payload: {parent_first_request}"
        );
        write_http_response(
            &mut parent_first_stream,
            r#"{
                "id": "chatcmpl_agent_parent_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_agent",
                            "type": "function",
                            "function": {
                                "name": "agent",
                                "arguments": "{\"prompt\":\"inspect child\",\"description\":\"Inspect child\",\"subagent_type\":\"explore\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut child_first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&child_first_stream);
        let child_first_request = read_http_request(&mut child_first_stream);
        assert!(
            child_first_request.contains("\"content\":\"inspect child\""),
            "missing child prompt in sub-agent payload: {child_first_request}"
        );
        for tool_name in ["read_file", "list_files", "grep", "bash"] {
            assert!(
                child_first_request.contains(&format!("\"name\":\"{tool_name}\"")),
                "missing {tool_name} tool definition in child payload: {child_first_request}"
            );
        }
        assert!(
            !child_first_request.contains("\"name\":\"agent\""),
            "child payload must not recursively expose agent tool: {child_first_request}"
        );
        for tool_name in ["write_file", "edit_file"] {
            assert!(
                !child_first_request.contains(&format!("\"name\":\"{tool_name}\"")),
                "explore child payload must not expose {tool_name}: {child_first_request}"
            );
        }
        write_http_response(
            &mut child_first_stream,
            r#"{
                "id": "chatcmpl_agent_child_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_child_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{\"path\":\"status.txt\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );

        let (mut child_second_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&child_second_stream);
        let child_second_request = read_http_request(&mut child_second_stream);
        assert!(
            child_second_request.contains("\"tool_call_id\":\"call_child_read\""),
            "missing child read_file result in sub-agent continuation: {child_second_request}"
        );
        assert!(
            child_second_request.contains("alpha child"),
            "missing read_file content in sub-agent continuation: {child_second_request}"
        );
        write_http_response(
            &mut child_second_stream,
            r#"{
                "id": "chatcmpl_agent_child_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "child answer"}
                }],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8}
            }"#,
        );

        let (mut parent_second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&parent_second_stream);
        let parent_second_request = read_http_request(&mut parent_second_stream);
        assert!(
            parent_second_request.contains("\"tool_call_id\":\"call_agent\""),
            "missing agent tool result in parent continuation: {parent_second_request}"
        );
        assert!(
            parent_second_request.contains("child answer"),
            "missing child result in parent continuation: {parent_second_request}"
        );
        assert!(
            parent_second_request.contains("[Agent stats: 1 tool calls, 26 tokens]"),
            "missing child tool-use stats in parent continuation: {parent_second_request}"
        );
        write_http_response(
            &mut parent_second_stream,
            r#"{
                "id": "chatcmpl_agent_parent_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "parent final"}
                }],
                "usage": {"prompt_tokens": 9, "completion_tokens": 10}
            }"#,
        );
    });

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

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("delegate")
        .arg("--allowed-tools")
        .arg("agent")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "parent final\n",
        "stdout"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_sub_agent_denies_prompted_write_permission_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-agent-deny-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-agent-deny-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut parent_first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&parent_first_stream);
        let parent_first_request = read_http_request(&mut parent_first_stream);
        assert!(
            parent_first_request.contains("\"name\":\"agent\""),
            "missing agent tool definition in parent payload: {parent_first_request}"
        );
        write_http_response(
            &mut parent_first_stream,
            r#"{
                "id": "chatcmpl_agent_deny_parent_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_agent",
                            "type": "function",
                            "function": {
                                "name": "agent",
                                "arguments": "{\"prompt\":\"write child file\",\"description\":\"Write child\",\"subagent_type\":\"general-purpose\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut child_first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&child_first_stream);
        let child_first_request = read_http_request(&mut child_first_stream);
        assert!(
            child_first_request.contains("\"content\":\"write child file\""),
            "missing child prompt in sub-agent payload: {child_first_request}"
        );
        assert!(
            child_first_request.contains("\"name\":\"write_file\""),
            "general-purpose child should expose write_file: {child_first_request}"
        );
        write_http_response(
            &mut child_first_stream,
            r#"{
                "id": "chatcmpl_agent_deny_child_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_child_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"child-created.txt\",\"content\":\"blocked\\n\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );

        let (mut child_second_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&child_second_stream);
        let child_second_request = read_http_request(&mut child_second_stream);
        assert!(
            child_second_request.contains("\"tool_call_id\":\"call_child_write\""),
            "missing child write_file result in sub-agent continuation: {child_second_request}"
        );
        assert!(
            child_second_request.contains("Permission denied."),
            "sub-agent write_file ask permission should be denied: {child_second_request}"
        );
        assert!(
            !child_second_request.contains("Successfully wrote"),
            "sub-agent write_file unexpectedly executed: {child_second_request}"
        );
        write_http_response(
            &mut child_second_stream,
            r#"{
                "id": "chatcmpl_agent_deny_child_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "child saw denial"}
                }],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8}
            }"#,
        );

        let (mut parent_second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&parent_second_stream);
        let parent_second_request = read_http_request(&mut parent_second_stream);
        assert!(
            parent_second_request.contains("\"tool_call_id\":\"call_agent\""),
            "missing agent tool result in parent continuation: {parent_second_request}"
        );
        assert!(
            parent_second_request.contains("child saw denial"),
            "missing denied child result in parent continuation: {parent_second_request}"
        );
        write_http_response(
            &mut parent_second_stream,
            r#"{
                "id": "chatcmpl_agent_deny_parent_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "parent final"}
                }],
                "usage": {"prompt_tokens": 9, "completion_tokens": 10}
            }"#,
        );
    });

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

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("delegate")
        .arg("--allowed-tools")
        .arg("agent")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    assert!(
        !workspace_dir.join("child-created.txt").exists(),
        "sub-agent write_file should not create files when permission asks"
    );
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "parent final\n",
        "stdout"
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_disallowed_tools_deny_provider_tool_call() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-deny-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-deny-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    fs::write(workspace_dir.join("status.txt"), "alpha\n")
        .expect("workspace file should be written");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"name\":\"read_file\""),
            "missing read_file tool definition in first payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_tool_deny_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{\"path\":\"status.txt\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"tool_call_id\":\"call_read\""),
            "missing denied tool result call id in second payload: {second_request}"
        );
        assert!(
            second_request.contains("Permission denied."),
            "missing denied tool result content in second payload: {second_request}"
        );
        assert!(
            !second_request.contains("alpha"),
            "disallowed read_file unexpectedly executed: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_tool_deny_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "blocked"}
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );
    });

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

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("read status")
        .arg("--disallowed-tools")
        .arg("read_file")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "blocked\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn headless_cli_auto_approves_default_write_tool_call() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-auto-approve-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-cli-auto-approve-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&first_stream);
        let first_request = read_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"name\":\"write_file\""),
            "missing write_file tool definition in first payload: {first_request}"
        );
        write_http_response(
            &mut first_stream,
            r#"{
                "id": "chatcmpl_tool_allow_1",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "id": "call_write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{\"path\":\"created.txt\",\"content\":\"beta\\n\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}
            }"#,
        );

        let (mut second_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&second_stream);
        let second_request = read_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"tool_call_id\":\"call_write\""),
            "missing write tool result call id in second payload: {second_request}"
        );
        assert!(
            second_request.contains("Successfully wrote 1 lines"),
            "missing successful write result content in second payload: {second_request}"
        );
        assert!(
            !second_request.contains("Permission denied."),
            "headless write_file ask permission was not auto-approved: {second_request}"
        );
        write_http_response(
            &mut second_stream,
            r#"{
                "id": "chatcmpl_tool_allow_2",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "written"}
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6}
            }"#,
        );
    });

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

    let output = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("--prompt")
        .arg("write status")
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .env_remove("IAC_CODE_RS_FAKE_PROVIDER")
        .env_remove("IAC_CODE_RS_FAKE_SCENARIO")
        .env_remove("IAC_CODE_PROVIDER")
        .env_remove("IAC_CODE_MODEL")
        .env_remove("IAC_CODE_BASE_URL")
        .env_remove("IAC_CODE_API_KEY")
        .current_dir(&workspace_dir)
        .output()
        .expect("command runs");

    server.join().expect("server thread");
    assert_eq!(
        fs::read_to_string(workspace_dir.join("created.txt"))
            .expect("created file should be readable"),
        "beta\n"
    );
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&workspace_dir).ok();

    assert_eq!(output.status.code(), Some(0), "exit code");
    assert_eq!(String::from_utf8_lossy(&output.stdout), "written\n");
    assert_eq!(String::from_utf8_lossy(&output.stderr), "", "stderr");
}

#[test]
fn a2a_websocket_server_config_ws_path_updates_agent_card_interface() {
    let config_dir = unique_temp_dir("iac-code-rs-cli-a2a-websocket-config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let port_probe = TcpListener::bind("127.0.0.1:0").expect("bind available port probe");
    let port = port_probe.local_addr().expect("probe addr").port();
    drop(port_probe);
    let config_path = config_dir.join("a2a.yml");
    fs::write(
        &config_path,
        format!("host: 127.0.0.1\nport: {port}\ntransport: websocket\nws_path: /custom/a2a\n"),
    )
    .expect("a2a config should be written");

    let mut child = Command::new(env!("CARGO_BIN_EXE_iac-code"))
        .arg("a2a")
        .arg("--config")
        .arg(&config_path)
        .env("IAC_CODE_CONFIG_DIR", &config_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("a2a server starts");

    let mut stream = connect_with_timeout(("127.0.0.1", port));
    websocket_client_handshake(&mut stream, port, "/custom/a2a");
    write_masked_websocket_text_frame(
        &mut stream,
        r#"{"jsonrpc":"2.0","id":"card","method":"agent/getAuthenticatedExtendedCard","params":{}}"#,
    );
    let response = read_unmasked_websocket_text_frame(&mut stream);
    child.kill().ok();
    child.wait().ok();
    fs::remove_dir_all(&config_dir).ok();

    assert!(
        response.contains(&format!(r#""url":"ws://127.0.0.1:{port}/custom/a2a""#)),
        "custom websocket path missing from agent card: {response}"
    );
    assert!(
        response.contains(r#""protocolBinding":"websocket""#),
        "websocket interface missing from agent card: {response}"
    );
    assert!(
        !response.contains(&format!(r#""url":"ws://127.0.0.1:{port}/a2a""#)),
        "default websocket path leaked into custom config agent card: {response}"
    );
}

fn assert_fixture(name: &str, scenario: Option<&str>) {
    let expected = command_fixture("headless_fake", name);
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_iac-code"));
    cmd.args(&expected.argv)
        .env("IAC_CODE_RS_FAKE_PROVIDER", "1")
        .env_remove("IAC_CODE_CONFIG_DIR")
        .env_remove("IAC_CODE_API_KEY")
        .env_remove("IAC_CODE_PROVIDER");
    if let Some(scenario) = scenario {
        cmd.env("IAC_CODE_RS_FAKE_SCENARIO", scenario);
    } else {
        cmd.env_remove("IAC_CODE_RS_FAKE_SCENARIO");
    }
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

fn normalize_workspace_path(text: &str, workspace_dir: &Path) -> String {
    let mut normalized = text.to_owned();
    if let Ok(canonical) = fs::canonicalize(workspace_dir) {
        let canonical = canonical.to_string_lossy();
        normalized = normalized.replace(canonical.as_ref(), "$WORKSPACE");
    }
    let workspace = workspace_dir.to_string_lossy();
    normalized.replace(workspace.as_ref(), "$WORKSPACE")
}

fn strip_ansi_sequences(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(character) = chars.next() {
        if character == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for next in chars.by_ref() {
                if next.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            output.push(character);
        }
    }
    output
}

fn assert_contains_in_order(haystack: &str, needles: &[&str]) {
    let mut offset = 0;
    for needle in needles {
        let remaining = &haystack[offset..];
        let position = remaining.find(needle).unwrap_or_else(|| {
            panic!("missing {needle:?} after byte {offset} in output:\n{haystack}")
        });
        offset += position + needle.len();
    }
}

fn line_suffix_with_prefix<'a>(lines: &'a [&str], prefix: &str, label: &str) -> &'a str {
    lines
        .iter()
        .find_map(|line| line.strip_prefix(prefix))
        .unwrap_or_else(|| panic!("{label}: missing prefix {prefix:?} in {lines:?}"))
}

fn configure_test_stream(stream: &std::net::TcpStream) {
    stream
        .set_nonblocking(false)
        .expect("accepted stream should be blocking");
    stream
        .set_read_timeout(Some(TEST_SERVER_TIMEOUT))
        .expect("accepted stream should have read timeout");
}

fn accept_with_timeout(listener: TcpListener) -> (std::net::TcpStream, std::net::SocketAddr) {
    let deadline = Instant::now() + TEST_SERVER_TIMEOUT;
    loop {
        match listener.accept() {
            Ok(value) => return value,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    panic!("timed out waiting for test server request");
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => panic!("failed to accept test server request: {error}"),
        }
    }
}

fn write_http_response(stream: &mut impl Write, body: &str) {
    write!(
        stream,
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
        body.len(),
        body
    )
    .expect("write response");
}

fn write_provider_config(config_dir: &Path, addr: std::net::SocketAddr) {
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

fn connect_with_timeout(addr: (&str, u16)) -> std::net::TcpStream {
    let addr = format!("{}:{}", addr.0, addr.1);
    let deadline = Instant::now() + TEST_SERVER_TIMEOUT;
    loop {
        match std::net::TcpStream::connect(&addr) {
            Ok(stream) => {
                stream
                    .set_read_timeout(Some(TEST_SERVER_TIMEOUT))
                    .expect("stream should have read timeout");
                stream
                    .set_write_timeout(Some(TEST_SERVER_TIMEOUT))
                    .expect("stream should have write timeout");
                return stream;
            }
            Err(error) => {
                assert!(
                    Instant::now() < deadline,
                    "timed out connecting to {addr}: {error}"
                );
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

fn websocket_client_handshake(stream: &mut std::net::TcpStream, port: u16, path: &str) {
    let request = format!(
        "GET {path} HTTP/1.1\r\n\
         Host: 127.0.0.1:{port}\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
         Sec-WebSocket-Version: 13\r\n\
         \r\n"
    );
    stream
        .write_all(request.as_bytes())
        .expect("websocket handshake request should be written");
    let headers = read_http_request(stream);
    assert!(
        headers.starts_with("HTTP/1.1 101 Switching Protocols"),
        "websocket handshake failed: {headers}"
    );
}

fn write_masked_websocket_text_frame(stream: &mut std::net::TcpStream, text: &str) {
    let payload = text.as_bytes();
    let mask = [0x11_u8, 0x22, 0x33, 0x44];
    let mut frame = Vec::with_capacity(payload.len() + 14);
    frame.push(0x81);
    if payload.len() < 126 {
        frame.push(0x80 | payload.len() as u8);
    } else if u16::try_from(payload.len()).is_ok() {
        frame.push(0x80 | 126);
        frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    } else {
        frame.push(0x80 | 127);
        frame.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    }
    frame.extend_from_slice(&mask);
    for (index, byte) in payload.iter().enumerate() {
        frame.push(byte ^ mask[index % mask.len()]);
    }
    stream
        .write_all(&frame)
        .and_then(|_| stream.flush())
        .expect("websocket text frame should be written");
}

fn read_unmasked_websocket_text_frame(stream: &mut std::net::TcpStream) -> String {
    let mut header = [0_u8; 2];
    stream
        .read_exact(&mut header)
        .expect("websocket frame header should be read");
    assert_eq!(header[0] & 0x0f, 0x1, "expected websocket text frame");
    let masked = header[1] & 0x80 != 0;
    assert!(
        !masked,
        "server-to-client websocket frames should be unmasked"
    );
    let mut length = (header[1] & 0x7f) as usize;
    if length == 126 {
        let mut extended = [0_u8; 2];
        stream
            .read_exact(&mut extended)
            .expect("websocket extended length should be read");
        length = u16::from_be_bytes(extended) as usize;
    } else if length == 127 {
        let mut extended = [0_u8; 8];
        stream
            .read_exact(&mut extended)
            .expect("websocket extended length should be read");
        length = u64::from_be_bytes(extended) as usize;
    }
    let mut payload = vec![0_u8; length];
    stream
        .read_exact(&mut payload)
        .expect("websocket payload should be read");
    String::from_utf8(payload).expect("websocket payload should be utf8")
}

fn session_dirs_for(config_dir: &Path, workspace_dir: &Path) -> Vec<PathBuf> {
    let project_dir = config_dir
        .join("projects")
        .join(sanitize_path(&workspace_cwd(workspace_dir)));
    if !project_dir.exists() {
        return Vec::new();
    }
    fs::read_dir(project_dir)
        .expect("project session dir should be readable")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir() && path.join(SESSION_JSONL_FILENAME).exists())
        .collect()
}

fn workspace_cwd(workspace_dir: &Path) -> String {
    fs::canonicalize(workspace_dir)
        .unwrap_or_else(|_| workspace_dir.to_path_buf())
        .to_string_lossy()
        .into_owned()
}
