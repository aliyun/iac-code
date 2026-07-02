use std::collections::BTreeMap;
use std::fs;
use std::thread;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_config::paths::ConfigPaths;
use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock, TextBlock};
use iac_code_tools::MemoryManager;
use iac_code_tui::{
    terminal_display_width, CommandSuggestionProvider, HistorySearchState, InputHistory,
    ShellHistoryProvider,
};

use crate::raw_picker::{raw_picker_clear_sequence, RawPickerSearchQuery};
use crate::raw_prompt_context::RawPromptActionContext;
use crate::raw_prompt_input::{
    raw_prompt_persist_pasted_image, read_raw_interactive_prompt,
    read_raw_interactive_prompt_input_with_context_and_image_source,
    read_raw_interactive_prompt_with_providers, RawPromptPastedImage,
};
use crate::raw_prompt_renderer::{
    raw_prompt_clear_sequence, raw_prompt_clear_sequence_from_state, raw_prompt_render_output,
    raw_prompt_render_output_with_image_links, raw_prompt_render_output_with_overlay,
    raw_prompt_render_output_with_state, raw_prompt_repaint_clear_lines, RawPromptRenderState,
    RawPromptSuggestionOverlay,
};
use crate::raw_prompt_text::{
    raw_prompt_cursor_position, raw_prompt_image_links, raw_prompt_strip_ansi_sequences,
    raw_prompt_visual_line_count, RAW_PROMPT_PREFIX_STYLED,
};
use crate::raw_search::render_raw_history_search;
use crate::raw_suggestions::ConfigMemorySuggestionSource;
use crate::raw_transcript::render_raw_transcript_view;
use crate::test_support::{
    assert_bytes_contains, empty_skill_catalog, raw_ansi_screen_after_writes, raw_prompt_render,
    raw_prompt_render_with_ghost, raw_prompt_test_suggestion, raw_prompt_text_fragment,
    raw_visible_lines_from_terminal_output, read_fd_exact, read_fd_until_contains,
    terminal_mode_bytes, unique_temp_dir, write_fd, EnvVarGuard, PseudoTerminal,
    StaticRawPromptImageSource,
};

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_reader_submits_text_with_enter() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-basic");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("").len()),
        raw_prompt_render("")
    );
    write_fd(pty.master, b"h");
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("h").len()),
        raw_prompt_render("h")
    );
    for (byte, expected) in [
        (b'e', "he"),
        (b'l', "hel"),
        (b'l', "hell"),
        (b'o', "hello"),
        (b' ', "hello "),
        (b'r', "hello r"),
        (b'a', "hello ra"),
        (b'w', "hello raw"),
    ] {
        write_fd(pty.master, &[byte]);
        assert_eq!(
            read_fd_exact(pty.master, raw_prompt_render(expected).len()),
            raw_prompt_render(expected)
        );
    }
    write_fd(pty.master, b"\r");
    assert_eq!(read_fd_exact(pty.master, b"\r\n".len()), b"\r\n");
    assert_eq!(
        read_fd_exact(pty.master, expected_exit.len()),
        expected_exit
    );

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("hello raw".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repositions_terminal_cursor_after_left_arrow() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-cursor-left");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("").len()),
        raw_prompt_render("")
    );
    write_fd(pty.master, b"abc");
    read_fd_until_contains(pty.master, &raw_prompt_render("abc"));
    write_fd(pty.master, b"\x1b[D");
    read_fd_until_contains(pty.master, b"\x1b[1D");

    write_fd(pty.master, b"\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &expected_exit);
    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("abc".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_cursor_reposition_uses_display_width_after_cursor() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-cursor-wide");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("").len()),
        raw_prompt_render("")
    );
    write_fd(pty.master, "模型a".as_bytes());
    read_fd_until_contains(pty.master, &raw_prompt_render("模型a"));
    write_fd(pty.master, b"\x1b[D");
    read_fd_until_contains(pty.master, b"\x1b[1D");
    write_fd(pty.master, b"\x1b[D");
    read_fd_until_contains(pty.master, b"\x1b[3D");

    write_fd(pty.master, b"\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &expected_exit);
    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("模型a".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_command_ghost_and_accepts_tab() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-command");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/hel\t\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render_with_ghost("/hel", "p "));
    assert_bytes_contains(&output, &raw_prompt_text_fragment("/help "));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("/help ".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_path_ghost_and_accepts_tab() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-path");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(root.join("main.tf"), "").expect("fixture file should be written");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"@main\t\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render_with_ghost("@main", ".tf"));
    assert_bytes_contains(&output, &raw_prompt_text_fragment("@main.tf"));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("@main.tf".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_shell_history_ghost_and_accepts_tab() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-shell-history-root");
    let home = unique_temp_dir("iac-code-rs-raw-prompt-shell-history-home");
    fs::create_dir_all(&root).expect("root should exist");
    fs::create_dir_all(&home).expect("home should exist");
    let history_path = home.join(".zsh_history");
    fs::write(
        &history_path,
        "cargo test\nterraform apply\nterraform plan\n",
    )
    .expect("history should be written");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt_with_providers(
                slave,
                None,
                &root,
                vec![Box::new(ShellHistoryProvider::with_history_path(
                    Some(history_path),
                    100,
                ))],
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"!terr\t\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(
        &output,
        &raw_prompt_render_with_ghost("!terr", "aform plan"),
    );
    assert_bytes_contains(&output, &raw_prompt_text_fragment("!terraform plan"));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("!terraform plan".to_owned())
    );
    fs::remove_dir_all(&root).ok();
    fs::remove_dir_all(&home).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_memory_name_ghost_and_accepts_tab() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-memory-root");
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-memory-config");
    fs::create_dir_all(&root).expect("root should exist");
    fs::create_dir_all(&config_dir).expect("config should exist");
    let memory_dir = config_dir.join("memory");
    MemoryManager::new(&memory_dir)
        .expect("memory manager should initialize")
        .save(
            "user-role",
            "Prefer focused answers.",
            "user",
            "User role memory",
        )
        .expect("memory should save");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt_with_providers(
                slave,
                None,
                &root,
                vec![Box::new(
                    CommandSuggestionProvider::default_commands()
                        .with_memory_source(Box::new(ConfigMemorySuggestionSource { memory_dir })),
                )],
            )
            .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"/memory-folder user\t\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(
        &output,
        &raw_prompt_render_with_ghost("/memory-folder user", "-role"),
    );
    assert_bytes_contains(
        &output,
        &raw_prompt_text_fragment("/memory-folder user-role"),
    );
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("/memory-folder user-role".to_owned())
    );
    fs::remove_dir_all(&root).ok();
    fs::remove_dir_all(&config_dir).ok();
}

#[cfg(unix)]
#[test]
fn raw_history_search_reports_footer_in_rendered_line_count() {
    let pty = PseudoTerminal::open();
    let state = HistorySearchState::new(Vec::new(), 5);

    let query = RawPickerSearchQuery::new();
    let line_count = render_raw_history_search(pty.slave, 0, &query, &state)
        .expect("history search should render");
    let output = read_fd_until_contains(pty.master, b"Enter select");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);

    assert_eq!(visible_lines.len(), 2);
    assert_eq!(line_count, visible_lines.len());
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_r_selects_history_search_result() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-history-search");
    fs::create_dir_all(&root).expect("root should exist");
    let mut history = InputHistory::new(root.join("history.txt"));
    history
        .append("explain vpc template")
        .expect("append history");
    history.append("deploy stack").expect("append history");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt_with_providers(slave, Some(&mut history), &root, vec![])
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"\x12deploy\r\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, b"history> deploy");
    assert_bytes_contains(&output, b"> deploy stack");
    assert_bytes_contains(&output, &raw_prompt_render("deploy stack"));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("deploy stack".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_r_clears_reflowed_prompt_after_terminal_narrows() {
    let pty = PseudoTerminal::open();
    pty.set_size(24, 24);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-history-search-resize");
    fs::create_dir_all(&root).expect("root should exist");
    let long_prompt = "abcdefghijklmnopqrst";

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt_with_providers(slave, None, &root, vec![])
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("").len()),
        raw_prompt_render("")
    );
    write_fd(pty.master, long_prompt.as_bytes());
    read_fd_until_contains(pty.master, &raw_prompt_render(long_prompt));

    pty.set_size(24, 6);
    write_fd(pty.master, b"\x12");
    let history_output = read_fd_until_contains(pty.master, b"his...");
    let expected_clear = raw_prompt_clear_sequence_from_state(RawPromptRenderState {
        line_count: raw_prompt_visual_line_count(long_prompt, "", 6),
        cursor_row: 0,
        rendered: None,
    });
    assert_bytes_contains(&history_output, expected_clear.as_bytes());

    write_fd(pty.master, b"\x1b");
    read_fd_until_contains(pty.master, &raw_prompt_render(long_prompt));
    write_fd(pty.master, b"\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &expected_exit);
    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some(long_prompt.to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_r_clears_multiline_paste() {
    let pty = PseudoTerminal::open();
    pty.set_size(24, 80);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-history-search-multiline-paste");
    fs::create_dir_all(&root).expect("root should exist");
    let pasted_prompt = "first line\nsecond line";

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt_with_providers(slave, None, &root, vec![])
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    assert_eq!(
        read_fd_exact(pty.master, raw_prompt_render("").len()),
        raw_prompt_render("")
    );
    write_fd(pty.master, b"\x1b[200~first line\nsecond line\x1b[201~");
    read_fd_until_contains(pty.master, b"second line");

    write_fd(pty.master, b"\x12");
    let history_output = read_fd_until_contains(pty.master, b"history> ");
    assert_bytes_contains(&history_output, raw_prompt_clear_sequence(2).as_bytes());

    write_fd(pty.master, b"\x1b");
    read_fd_until_contains(pty.master, b"second line");
    write_fd(pty.master, b"\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &expected_exit);
    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some(pasted_prompt.to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_p_selects_quick_open_file() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-quick-open");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(root.join("main.tf"), "resource \"x\" \"y\" {}\n")
        .expect("fixture should be written");
    fs::write(root.join("README.md"), "# readme\n").expect("fixture should be written");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"\x10main\r\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, b"quick> main");
    assert_bytes_contains(&output, b"> main.tf");
    assert_bytes_contains(&output, &raw_prompt_render("@main.tf"));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("@main.tf".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_f_selects_global_search_result() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-global-search");
    fs::create_dir_all(&root).expect("root should exist");
    fs::write(
        root.join("main.tf"),
        "resource \"alicloud_vpc\" \"main\" {}\n",
    )
    .expect("fixture should be written");
    fs::write(root.join("README.md"), "no matching content\n").expect("fixture should be written");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"\x06alicloud\r\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, b"search> alicloud");
    assert_bytes_contains(
        &output,
        b"> main.tf:1  resource \"alicloud_vpc\" \"main\" {}",
    );
    assert_bytes_contains(
        &output,
        &raw_prompt_render("@main.tf:1  resource \"alicloud_vpc\" \"main\" {}"),
    );
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("@main.tf:1  resource \"alicloud_vpc\" \"main\" {}".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_v_attaches_image_content() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-image-paste");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let mut image_source = StaticRawPromptImageSource::new(vec![RawPromptPastedImage {
                media_type: "image/png".to_owned(),
                data: "base64-image".to_owned(),
                source_path: None,
            }]);
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &RawPromptActionContext::default(),
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
    write_fd(pty.master, b"\x16 describe\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render("[Image #1] describe"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "[Image #1] describe");
    assert_eq!(
        input.prompt_content,
        Some(AgentMessageContent::Blocks(vec![
            AgentContentBlock::Image(ImageBlock {
                media_type: "image/png".to_owned(),
                data: "base64-image".to_owned(),
            }),
            AgentContentBlock::Text(TextBlock {
                text: " describe".to_owned(),
            }),
        ]))
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_persists_pasted_images_across_prompts() {
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-image-persist");
    fs::create_dir_all(&root).expect("root should exist");

    let mut session_images: Vec<RawPromptPastedImage> = Vec::new();

    let pty = PseudoTerminal::open();
    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let mut images = std::mem::take(&mut session_images);
        move || {
            let mut image_source = StaticRawPromptImageSource::new(vec![RawPromptPastedImage {
                media_type: "image/png".to_owned(),
                data: "first-image".to_owned(),
                source_path: None,
            }]);
            let input = read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &RawPromptActionContext::default(),
                &mut image_source,
                &mut images,
            )
            .expect("raw prompt should read");
            (input, images)
        }
    });
    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"\x16 one\r");
    let _ = read_fd_until_contains(pty.master, &expected_exit);
    let (first, images) = handle.join().expect("reader thread should finish");
    let first = first.expect("prompt input should be returned");
    assert_eq!(first.text, "[Image #1] one");
    session_images = images;
    assert_eq!(session_images.len(), 1);

    let pty = PseudoTerminal::open();
    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        let mut images = std::mem::take(&mut session_images);
        move || {
            let mut image_source = StaticRawPromptImageSource::new(vec![RawPromptPastedImage {
                media_type: "image/png".to_owned(),
                data: "second-image".to_owned(),
                source_path: None,
            }]);
            let input = read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &RawPromptActionContext::default(),
                &mut image_source,
                &mut images,
            )
            .expect("raw prompt should read");
            (input, images)
        }
    });
    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"\x16 two\r");
    let _ = read_fd_until_contains(pty.master, &expected_exit);
    let (second, images) = handle.join().expect("reader thread should finish");
    let second = second.expect("prompt input should be returned");
    assert_eq!(second.text, "[Image #2] two");
    assert_eq!(images.len(), 2);
    assert_eq!(
        second.prompt_content,
        Some(AgentMessageContent::Blocks(vec![
            AgentContentBlock::Image(ImageBlock {
                media_type: "image/png".to_owned(),
                data: "second-image".to_owned(),
            }),
            AgentContentBlock::Text(TextBlock {
                text: " two".to_owned(),
            }),
        ]))
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_focus_in_renders_clipboard_image_hint() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    pty.set_size(8, 100);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-clipboard-hint");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let mut image_source = StaticRawPromptImageSource::new_with_has_image_results(
                vec![RawPromptPastedImage {
                    media_type: "image/png".to_owned(),
                    data: "base64-image".to_owned(),
                    source_path: None,
                }],
                vec![false, true],
            );
            read_raw_interactive_prompt_input_with_context_and_image_source(
                slave,
                None,
                &root,
                vec![],
                &RawPromptActionContext::default(),
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
    read_fd_until_contains(pty.master, &raw_prompt_render(""));
    write_fd(pty.master, b"\x1b[I");
    let output = read_fd_until_contains(pty.master, "剪贴板中有图像 · 按 ctrl+v 粘贴".as_bytes());
    assert_bytes_contains(&output, "剪贴板中有图像 · 按 ctrl+v 粘贴".as_bytes());
    write_fd(pty.master, b"describe\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render("describe"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "describe");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_ctrl_o_shows_transcript_and_returns_to_prompt() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-transcript");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let context = RawPromptActionContext {
                transcript_lines: vec![
                    "❯ explain vpc".to_owned(),
                    "Use ALIYUN::ECS::VPC.".to_owned(),
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
    write_fd(pty.master, b"\x0f");
    let transcript_output = read_fd_until_contains(pty.master, b"Showing transcript");
    assert_bytes_contains(&transcript_output, b"\x1b[?1049h");
    assert_bytes_contains(&transcript_output, "❯ explain vpc".as_bytes());
    assert_bytes_contains(&transcript_output, b"Use ALIYUN::ECS::VPC.");
    write_fd(pty.master, b"\x1b");
    let prompt_output = read_fd_until_contains(pty.master, &raw_prompt_render(""));
    assert_bytes_contains(&prompt_output, b"\x1b[?1049l");
    assert_bytes_contains(&prompt_output, &raw_prompt_render(""));
    write_fd(pty.master, b"next\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render("next"));
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "next");
    assert_eq!(input.prompt_content, None);
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_transcript_reflows_after_terminal_resize() {
    let pty = PseudoTerminal::open();
    pty.set_size(8, 40);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-transcript-resize");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            let context = RawPromptActionContext {
                transcript_lines: vec![
                    "abcdefghij klmnopqrst uvwxyz".to_owned(),
                    "done".to_owned(),
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
    write_fd(pty.master, b"\x0f");
    let wide_output = read_fd_until_contains(pty.master, b"Showing transcript");
    assert_bytes_contains(&wide_output, b"abcdefghij klmnopqrst uvwxyz");

    pty.set_size(8, 12);
    write_fd(pty.master, b"x");
    let narrow_output = read_fd_until_contains(pty.master, b"wxyz");
    assert_bytes_contains(&narrow_output, b"abcdefghij\r\nklmnopqrst\r\nuvwxyz");
    assert!(!String::from_utf8_lossy(&narrow_output).contains("abcdefghij klmnopqrst uvwxyz"));

    write_fd(pty.master, b"\x1b");
    read_fd_until_contains(pty.master, &raw_prompt_render(""));
    write_fd(pty.master, b"\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &expected_exit);

    let input = handle
        .join()
        .expect("reader thread should finish")
        .expect("prompt input should be returned");
    assert_eq!(input.text, "");
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_transcript_view_crops_to_current_terminal_height() {
    let pty = PseudoTerminal::open();
    pty.set_size(5, 20);
    let lines = vec![
        "one".to_owned(),
        "two".to_owned(),
        "three".to_owned(),
        "four".to_owned(),
        "five".to_owned(),
        "six".to_owned(),
    ];

    let rendered_lines =
        render_raw_transcript_view(pty.slave, &lines).expect("transcript should render");
    let output = read_fd_until_contains(pty.master, b"Showing transcript");

    assert_eq!(rendered_lines, 4);
    assert!(!raw_visible_lines_from_terminal_output(&output)
        .iter()
        .any(|line| line.contains("one")));
}

#[cfg(unix)]
#[test]
fn raw_transcript_view_draws_footer_on_terminal_bottom_row() {
    let pty = PseudoTerminal::open();
    pty.set_size(6, 24);
    let lines = vec!["❯ short".to_owned(), "answer".to_owned()];

    let rendered_lines =
        render_raw_transcript_view(pty.slave, &lines).expect("transcript should render");
    let output = read_fd_until_contains(pty.master, b"Showing transcript");

    assert_eq!(rendered_lines, 3);
    assert_bytes_contains(&output, b"\x1b[H\x1b[2J");
    assert_bytes_contains(&output, b"\x1b[6;1H\x1b[2K");
}

#[cfg(unix)]
#[test]
fn raw_transcript_view_writes_visible_body_lines_within_terminal_width() {
    let pty = PseudoTerminal::open();
    pty.set_size(8, 18);
    let lines = vec![
        "     Tool prompt should also wrap cleanly without stale wide rows".to_owned(),
        "Use ordinary words that should wrap cleanly before reaching the footer.".to_owned(),
        "❯ 请生成一个很长很长的专有网络模板".to_owned(),
    ];

    render_raw_transcript_view(pty.slave, &lines).expect("transcript should render");
    let output = read_fd_until_contains(pty.master, b"Showing transcript");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);
    let body_lines: Vec<_> = visible_lines
        .into_iter()
        .filter(|line| !line.contains("Showing transcript"))
        .collect();

    assert!(
        body_lines
            .iter()
            .all(|line| terminal_display_width(line) <= 18),
        "raw transcript body wrote a line wider than the terminal: {body_lines:?}"
    );
    assert!(
        body_lines.iter().any(|line| line.starts_with("❯ ")),
        "user prompt prefix should remain visible after wrapping: {body_lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repaint_clears_previous_wrapped_rows() {
    let (first, first_lines) = raw_prompt_render_output(0, "abcdef", "abcdef".len(), "", 4);
    assert_eq!(first, format!("\r\x1b[2K{RAW_PROMPT_PREFIX_STYLED}abcdef"));
    assert_eq!(first_lines, 2);

    let (second, second_lines) = raw_prompt_render_output(first_lines, "x", "x".len(), "", 4);
    assert_eq!(
        second,
        format!(
            "{}{}x",
            raw_picker_clear_sequence(first_lines - 1),
            RAW_PROMPT_PREFIX_STYLED
        )
    );
    assert_eq!(second_lines, 1);
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repaint_leaves_clean_ansi_screen_after_shrinking() {
    let (first, first_state) = raw_prompt_render_output_with_state(
        RawPromptRenderState::empty(),
        "abcdefghijklmnopqrst",
        "abcdefghijklmnopqrst".len(),
        "",
        24,
    );
    assert_eq!(first_state.line_count, 1);

    let (second, second_state) =
        raw_prompt_render_output_with_state(first_state, "x", "x".len(), "", 6);
    assert_eq!(second_state.line_count, 1);

    let screen = raw_ansi_screen_after_writes(6, 4, &[first.as_bytes(), second.as_bytes()]);

    assert_eq!(screen.lines[0], "❯ x");
    assert!(
        screen.lines[1..].iter().all(|line| line.trim().is_empty()),
        "repaint should leave no stale wrapped prompt rows on the terminal screen: {screen:?}"
    );
    assert_eq!(
        screen.cursor,
        (0, terminal_display_width("❯ x")),
        "cursor should return to the end of the final prompt"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repaint_clears_rows_after_terminal_narrows() {
    let (_wide, wide_lines) = raw_prompt_render_output(
        0,
        "abcdefghijklmnopqrst",
        "abcdefghijklmnopqrst".len(),
        "",
        24,
    );
    assert_eq!(wide_lines, 1);

    let (narrow, narrow_lines) = raw_prompt_render_output(
        wide_lines,
        "abcdefghijklmnopqrst",
        "abcdefghijklmnopqrst".len(),
        "",
        6,
    );
    assert_eq!(narrow_lines, 4);
    let expected_clear = raw_prompt_clear_sequence_from_state(RawPromptRenderState {
        line_count: narrow_lines,
        cursor_row: 0,
        rendered: None,
    });
    assert!(
        narrow.starts_with(&expected_clear),
        "repaint after terminal resize must clear the current wrapped height: {narrow:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repaint_starts_from_previous_cursor_row() {
    let previous = RawPromptRenderState {
        line_count: 2,
        cursor_row: 0,
        rendered: None,
    };
    let (output, state) = raw_prompt_render_output_with_state(previous, "abXcdef", 3, "", 4);

    assert!(
        output.starts_with(&format!(
            "\r\x1b[2K\r\n\x1b[2K\r\n\x1b[2K\x1b[2A\r{}abXcdef",
            RAW_PROMPT_PREFIX_STYLED
        )),
        "repaint should clear from the previous cursor row without moving above prompt: {output:?}"
    );
    assert_eq!(state.line_count, 3);
    assert_eq!(state.cursor_row, 1);
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_line_count_respects_explicit_newlines() {
    assert_eq!(raw_prompt_visual_line_count("hello\nworld", "", 80), 2);
    assert_eq!(raw_prompt_visual_line_count("abcdef\nxyz", "", 4), 3);

    let (_first, first_lines) =
        raw_prompt_render_output(0, "hello\nworld", "hello\nworld".len(), "", 80);
    assert_eq!(first_lines, 2);
    let (second, second_lines) = raw_prompt_render_output(first_lines, "x", "x".len(), "", 80);
    assert_eq!(
        second,
        format!(
            "{}{}x",
            raw_prompt_clear_sequence(first_lines),
            RAW_PROMPT_PREFIX_STYLED
        )
    );
    assert_eq!(second_lines, 1);
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_omits_ghost_text_for_multiline_input() {
    let (output, lines) = raw_prompt_render_output(
        0,
        "line1\nline2",
        "line1\nline2".len(),
        " should-not-render",
        80,
    );

    assert_eq!(lines, 2);
    assert!(output.contains("line1\r\nline2"));
    assert!(!output.contains("should-not-render"));
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_clear_count_ignores_multiline_ghost_text() {
    assert_eq!(
        raw_prompt_repaint_clear_lines(1, "line1\nline2", " ghost that would wrap", 8),
        2
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repositions_cursor_to_wrapped_previous_line() {
    let (output, lines) = raw_prompt_render_output(0, "abcdef", "ab".len(), "", 4);

    assert_eq!(lines, 2);
    assert!(
        output.ends_with("\x1b[1A\r\x1b[4C"),
        "cursor should move from the final wrapped row back to the logical cursor: {output:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repositions_cursor_to_explicit_previous_line() {
    let (output, lines) = raw_prompt_render_output(0, "hello\nworld", "hello".len(), "", 80);

    assert_eq!(lines, 2);
    assert!(
        output.ends_with("\x1b[1A\r\x1b[7C"),
        "cursor should move from after the final line back to the line before newline: {output:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_repositions_cursor_across_wrapped_wide_chars() {
    let (output, lines) = raw_prompt_render_output(0, "你好ab", "你".len(), "", 3);

    assert_eq!(lines, 4);
    assert!(
        output.ends_with("\x1b[2A\r\x1b[2C"),
        "cursor should move from the final wrapped row back to the wide-char cursor: {output:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_counts_emoji_graphemes_by_terminal_width() {
    let family = "👨‍👩‍👧‍👦";
    let text = format!("{family}a");
    let expected_cursor_column = terminal_display_width("❯ ") + terminal_display_width(family);

    assert_eq!(terminal_display_width(family), 2);
    assert_eq!(raw_prompt_visual_line_count(&text, "", 5), 1);

    let position = raw_prompt_cursor_position(&text, family.len(), 5);
    assert_eq!(position.line, 0);
    assert_eq!(position.column, expected_cursor_column);
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_submit_clears_suggestion_overlay() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en_US.UTF-8"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    pty.set_size(20, 100);
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-submit-overlay-clear");
    fs::create_dir_all(&root).expect("root should exist");

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, empty_skill_catalog())
                .expect("raw prompt should read")
        }
    });

    let mut output = Vec::new();
    output.extend(read_fd_exact(pty.master, expected_enter.len()));
    write_fd(pty.master, b"/compact\r");
    output.extend(read_fd_until_contains(pty.master, &expected_exit));
    let screen = raw_ansi_screen_after_writes(100, 20, &[&output]);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("/compact ".to_owned())
    );
    assert!(
        !screen
            .lines
            .iter()
            .any(|line| line.contains("Enter Confirm")),
        "submit should clear the suggestion help row: {screen:?}"
    );
    assert!(
        !screen
            .lines
            .iter()
            .any(|line| line.contains("compact") && line.contains("Compact conversation")),
        "submit should clear command suggestion rows: {screen:?}"
    );
    fs::remove_dir_all(root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_image_refs_as_highlighted_links() {
    let mut image_links = BTreeMap::new();
    image_links.insert(
        1,
        "file:///tmp/iac-code-image-cache/session/1.png".to_owned(),
    );

    let (output, lines) = raw_prompt_render_output_with_image_links(
        0,
        "[Image #1] describe",
        "[Image #1]".len(),
        "",
        80,
        &image_links,
    );

    assert_eq!(lines, 1);
    assert!(
        output.contains("\x1b]8;;file:///tmp/iac-code-image-cache/session/1.png\x1b\\"),
        "{output:?}"
    );
    assert!(
        output.contains("\x1b[1m\x1b[96m[Image #1]\x1b[0m"),
        "{output:?}"
    );
    assert!(
        raw_prompt_strip_ansi_sequences(&output).contains("❯ [Image #1] describe"),
        "{output:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_persists_pasted_images_for_clickable_links() {
    let root = unique_temp_dir("iac-code-rs-pasted-image-cache");
    let config_dir = root.join("config");
    let paths = ConfigPaths {
        credentials_path: config_dir.join(".credentials.yml"),
        settings_path: config_dir.join("settings.yml"),
        cloud_credentials_path: config_dir.join(".cloud-credentials.yml"),
        history_path: config_dir.join(".input_history"),
        config_dir: config_dir.clone(),
    };
    let context = RawPromptActionContext {
        current_session_id: Some("session-image".to_owned()),
        config_paths: Some(paths),
        ..RawPromptActionContext::default()
    };
    let mut image = RawPromptPastedImage {
        media_type: "image/png".to_owned(),
        data: STANDARD.encode(b"png-bytes"),
        source_path: None,
    };

    raw_prompt_persist_pasted_image(&mut image, 1, &context);

    let expected_path = config_dir.join("image-cache/session-image/1.png");
    assert_eq!(image.source_path.as_deref(), Some(expected_path.as_path()));
    assert_eq!(
        fs::read(&expected_path).expect("pasted image should be cached"),
        b"png-bytes"
    );
    let links = raw_prompt_image_links(&[image]);
    let expected_uri = format!("file://{}", expected_path.to_string_lossy());
    assert_eq!(
        links.get(&1).map(String::as_str),
        Some(expected_uri.as_str())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_renders_suggestion_overlay_and_restores_cursor() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en_US.UTF-8"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
    ]);
    let suggestions = vec![
        raw_prompt_test_suggestion("help", "/help ", "Show help"),
        raw_prompt_test_suggestion("hello", "/hello ", "Say hello"),
    ];
    let overlay = RawPromptSuggestionOverlay {
        visible: &suggestions,
        selected_index: 1,
        has_more_above: false,
        has_more_below: true,
    };

    let (output, state) = raw_prompt_render_output_with_overlay(
        RawPromptRenderState::empty(),
        "/h",
        "/h".len(),
        "elp ",
        40,
        overlay,
    );
    let visible_lines = raw_visible_lines_from_terminal_output(output.as_bytes());

    assert!(output.contains("help"));
    assert!(output.contains("hello"));
    assert!(output.contains("Enter Confirm"));
    assert!(output.ends_with("\x1b[3A\r\x1b[4C"));
    assert_eq!(state.line_count, 4);
    assert_eq!(state.cursor_row, 0);
    assert!(
        visible_lines
            .iter()
            .all(|line| terminal_display_width(line) <= 40),
        "suggestion overlay should fit terminal width: {visible_lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_suggestion_overlay_localizes_hint_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let suggestions = vec![raw_prompt_test_suggestion("exit", "/exit", "Exit")];
    let overlay = RawPromptSuggestionOverlay {
        visible: &suggestions,
        selected_index: 0,
        has_more_above: false,
        has_more_below: false,
    };

    let (output, _state) = raw_prompt_render_output_with_overlay(
        RawPromptRenderState::empty(),
        "/e",
        "/e".len(),
        "xit ",
        60,
        overlay,
    );

    assert!(output.contains("↑↓ 导航"), "{output:?}");
    assert!(output.contains("Enter 确认"), "{output:?}");
    assert!(output.contains("Tab 填充"), "{output:?}");
    assert!(output.contains("Esc 关闭"), "{output:?}");
}
