use std::fs;
#[cfg(unix)]
use std::thread;

use crate::raw_prompt_context::RawPromptActionContext;
use crate::raw_prompt_input::{
    read_raw_interactive_prompt, read_raw_interactive_prompt_input_with_context_and_image_source,
    RawPromptPastedImage,
};
use crate::raw_suggestions::raw_interactive_skill_catalog;
use crate::test_support::{
    assert_bytes_contains, raw_prompt_render, raw_prompt_render_with_ghost,
    raw_prompt_text_fragment, read_fd_exact, read_fd_until_contains, terminal_mode_bytes,
    unique_temp_dir, write_fd, EnvVarGuard, PseudoTerminal, StaticRawPromptImageSource,
};

#[cfg(unix)]
#[test]
fn raw_interactive_prompt_initial_render_shows_clipboard_image_hint() {
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
    let root = unique_temp_dir("iac-code-rs-raw-prompt-initial-clipboard-hint");
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
fn raw_interactive_prompt_renders_skill_ghost_and_accepts_tab() {
    let pty = PseudoTerminal::open();
    let expected_enter = terminal_mode_bytes(iac_code_tui::terminal_mode_enter_sequences());
    let expected_exit = terminal_mode_bytes(iac_code_tui::terminal_mode_exit_sequences());
    let root = unique_temp_dir("iac-code-rs-raw-prompt-skill");
    fs::create_dir_all(&root).expect("root should exist");
    let skills = sample_skill_catalog();

    let handle = thread::spawn({
        let slave = pty.slave;
        let root = root.clone();
        move || {
            read_raw_interactive_prompt(slave, None, &root, skills).expect("raw prompt should read")
        }
    });

    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"$simp\t\r");
    let output = read_fd_until_contains(pty.master, &expected_exit);
    assert_bytes_contains(&output, &raw_prompt_render_with_ghost("$simp", "lify "));
    assert_bytes_contains(&output, &raw_prompt_text_fragment("$simplify "));
    assert_bytes_contains(&output, &expected_exit);

    assert_eq!(
        handle.join().expect("reader thread should finish"),
        Some("$simplify ".to_owned())
    );
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
#[test]
fn raw_interactive_skill_catalog_omits_disabled_and_non_user_invocable_skills() {
    let config_dir = unique_temp_dir("iac-code-rs-raw-prompt-skill-config");
    let root = unique_temp_dir("iac-code-rs-raw-prompt-skill-catalog");
    fs::create_dir_all(&config_dir).expect("config dir should exist");
    fs::create_dir_all(root.join(".iac-code").join("skills"))
        .expect("project skills dir should exist");
    fs::write(
        config_dir.join("settings.yml"),
        "disabled_skills:\n- disabled-helper\n",
    )
    .expect("settings should be written");
    for (name, frontmatter) in [
        ("enabled-helper", "description: Enabled helper"),
        ("disabled-helper", "description: Disabled helper"),
        (
            "auto-helper",
            "description: Auto helper\nuser_invocable: false",
        ),
    ] {
        let skill_dir = root.join(".iac-code").join("skills").join(name);
        fs::create_dir_all(&skill_dir).expect("skill dir should exist");
        fs::write(
            skill_dir.join("SKILL.md"),
            format!("---\n{frontmatter}\n---\n\nInstructions.\n"),
        )
        .expect("skill should be written");
    }

    let _env = EnvVarGuard::set(
        "IAC_CODE_CONFIG_DIR",
        config_dir
            .to_str()
            .expect("config dir should be valid unicode"),
    );
    let catalog = raw_interactive_skill_catalog(&root);
    let names = catalog
        .get_all()
        .into_iter()
        .map(|skill| skill.name)
        .collect::<Vec<_>>();

    assert!(names.contains(&"enabled-helper".to_owned()));
    assert!(names.contains(&"simplify".to_owned()));
    assert!(!names.contains(&"disabled-helper".to_owned()));
    assert!(!names.contains(&"auto-helper".to_owned()));
    assert!(!names.contains(&"iac-aliyun".to_owned()));
    fs::remove_dir_all(&config_dir).ok();
    fs::remove_dir_all(&root).ok();
}

#[cfg(unix)]
fn sample_skill_catalog() -> iac_code_tui::SkillCatalog {
    let mut catalog = iac_code_tui::SkillCatalog::new();
    catalog.register(iac_code_tui::SkillDefinition {
        name: "simplify".to_owned(),
        description: "Review changed code and simplify issues found.".to_owned(),
        aliases: Vec::new(),
        hidden: false,
    });
    catalog
}
