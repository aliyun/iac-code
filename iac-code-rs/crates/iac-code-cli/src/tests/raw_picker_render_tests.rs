use std::collections::BTreeSet;
use std::fs;

use iac_code_core::sanitize_path;
use iac_code_tools::{SkillDefinition, SkillSource};
use iac_code_tui::{
    terminal_display_width, CommandSuggestionProvider, EffortLevel, ModelDefinition,
    ModelPickerState, ModelProviderGroup, ModelThinkingSpec, PromptKeyEvent, SkillManagementItem,
    SkillManagementSource, SkillsPickerState, SuggestionProvider,
};

use crate::cli_args::Cli;
use crate::interactive_prompt_handler::interactive_slash_command_history_mode;
use crate::interactive_status::interactive_status_message;
use crate::raw_effort::raw_effort_picker_render_output;
use crate::raw_memory::{memory_runtime_paths, raw_memory_dialog_render_output};
use crate::raw_model_effort::raw_model_picker_render_output;
use crate::raw_picker::{
    raw_picker_fit_line_to_width, raw_picker_query_prompt_line, RawPickerSearchQuery,
};
use crate::raw_prompt_context::RawPromptActionContext;
use crate::raw_skills::render_raw_skills_picker;
use crate::raw_suggestions::{
    localized_command_catalog, raw_interactive_command_suggestion_provider,
};
use crate::session_utils::find_git_worktree_root;
use crate::skills_management::{format_skill_token_estimate, skill_management_item};
use crate::test_support::{
    assert_bytes_contains, english_locale_guard, paths_for, raw_ansi_screen_after_writes,
    raw_strip_ansi_sequences, raw_visible_lines_from_terminal_output, read_fd_until_contains,
    unique_temp_dir, EnvVarGuard, PseudoTerminal,
};

#[cfg(unix)]
#[test]
fn interactive_status_message_uses_python_panel_styles() {
    let root = unique_temp_dir("iac-code-rs-status-panel-styles");
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("IAC_CODE_CONFIG_DIR", root.to_string_lossy().as_ref()),
    ]);
    let cli = Cli {
        model: "qwen3.7-plus".to_owned(),
        max_turns: 100,
        ..Cli::default()
    };

    let output = interactive_status_message(&cli, 0, Some("session-id"), 0, false);

    assert!(output.starts_with("\x1b[36m╭"), "{output:?}");
    assert!(output.contains(" Session Status "), "{output:?}");
    assert!(output.contains("\x1b[36m│\x1b[0m"), "{output:?}");
    assert!(output.contains("\x1b[36m╰"), "{output:?}");
    assert!(output.contains("\x1b[1mSession:\x1b[0m"), "{output:?}");
    assert!(
        output.contains("\x1b[1mAPI Token Usage (recorded):\x1b[0m"),
        "{output:?}"
    );
    assert!(
        output.contains("\x1b[2mNo recorded API usage for this session yet.\x1b[0m"),
        "{output:?}"
    );

    let plain = raw_strip_ansi_sequences(&output);
    assert!(plain.contains("Session Status"), "{plain:?}");
    assert!(plain.contains("Session:"), "{plain:?}");
    fs::remove_dir_all(root).ok();
}

#[cfg(unix)]
#[test]
fn raw_model_picker_renders_shared_select_style_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let state = ModelPickerState::new(
        "qwen3.7-plus",
        vec![ModelProviderGroup::new(
            "dashscope",
            "Alibaba Cloud Bailian",
            vec![
                ModelDefinition::new("qwen3.7-max", ModelThinkingSpec::none()),
                ModelDefinition::new("qwen3.7-plus", ModelThinkingSpec::none()),
                ModelDefinition::new("qwen3.6-plus", ModelThinkingSpec::none()),
            ],
        )],
    );

    let (output, line_count) = raw_model_picker_render_output(0, &state, "qwen3.7-plus", false, 80);
    let screen = raw_ansi_screen_after_writes(80, 10, &[output.as_bytes()]);

    assert_eq!(line_count, 9);
    assert!(!output.contains("model>"), "{output:?}");
    assert_eq!(screen.lines[1], "  为 Alibaba Cloud Bailian 选择模型");
    assert_eq!(screen.lines[3], "    qwen3.7-max");
    assert_eq!(screen.lines[4], "  > qwen3.7-plus (当前)");
    assert_eq!(screen.lines[5], "    qwen3.6-plus");
    assert_eq!(screen.lines[6], "    自定义模型...");
    assert_eq!(screen.lines[8], "  ↑↓ 导航  Enter 确认  Esc 返回");
    assert!(output.contains("\x1b[96m> qwen3.7-plus (当前)\x1b[0m"));
    assert!(output.contains("\x1b[38;2;128;128;128mqwen3.7-max\x1b[0m"));
}

#[cfg(unix)]
#[test]
fn raw_effort_picker_renders_shared_select_style_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let context = RawPromptActionContext {
        effort_model: "deepseek-v4-pro".to_owned(),
        effort_allowed: vec![EffortLevel::High, EffortLevel::Max],
        effort_current: Some(EffortLevel::High),
        ..RawPromptActionContext::default()
    };

    let (output, line_count) = raw_effort_picker_render_output(0, 0, &context, 80);
    let screen = raw_ansi_screen_after_writes(80, 8, &[output.as_bytes()]);

    assert_eq!(line_count, 7);
    assert!(!output.contains("effort>"), "{output:?}");
    assert_eq!(screen.lines[1], "  为 deepseek-v4-pro 选择思考强度");
    assert_eq!(screen.lines[3], "  > ◆◆◆ high");
    assert_eq!(screen.lines[4], "    ◆◆◆◆◆ max");
    assert_eq!(screen.lines[6], "  ↑↓ 导航  Enter 确认  Esc 返回");
    assert!(output.contains("\x1b[96m> ◆◆◆ high\x1b[0m"));
    assert!(output.contains("\x1b[38;2;128;128;128m◆◆◆◆◆ max\x1b[0m"));
}

#[cfg(unix)]
#[test]
fn raw_interactive_command_suggestions_localize_descriptions_like_python() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let provider = raw_interactive_command_suggestion_provider();
    let suggestions = provider.provide(&iac_code_tui::CompletionToken {
        text: "/".to_owned(),
        start: 0,
        end: 1,
        trigger: "/".to_owned(),
    });
    let auth = suggestions
        .iter()
        .find(|item| item.display_text == "auth")
        .expect("auth suggestion should be present");

    assert_eq!(auth.description.as_deref(), Some("配置 LLM 提供商认证"));
}

#[cfg(unix)]
#[test]
fn raw_interactive_command_catalog_omits_tasks_like_python() {
    let catalog = localized_command_catalog();
    assert!(
        catalog
            .get_all()
            .iter()
            .all(|command| command.name != "tasks"),
        "the interactive slash catalog should not expose /tasks"
    );

    let provider = CommandSuggestionProvider::new(catalog);
    let suggestions = provider.provide(&iac_code_tui::CompletionToken {
        text: "/task".to_owned(),
        start: 0,
        end: 5,
        trigger: "/".to_owned(),
    });

    assert!(
        suggestions.iter().all(|item| item.display_text != "tasks"),
        "/task suggestions should not include /tasks: {suggestions:?}"
    );
    assert_eq!(interactive_slash_command_history_mode("/tasks"), None);
}

#[cfg(unix)]
#[test]
fn raw_picker_line_truncates_to_terminal_display_width() {
    assert_eq!(raw_picker_fit_line_to_width("模型模型模型", 8), "模型...");
    assert_eq!(
        terminal_display_width(&raw_picker_fit_line_to_width("模型模型模型", 8)),
        7
    );
    assert_eq!(
        raw_picker_fit_line_to_width("provider 模型", 12),
        "provider ..."
    );
}

#[cfg(unix)]
#[test]
fn raw_picker_line_truncates_by_grapheme_cluster_display_width() {
    let family = "👨‍👩‍👧‍👦";
    let fitted = raw_picker_fit_line_to_width(&format!("{family}abcdef"), 5);

    assert_eq!(fitted, format!("{family}..."));
    assert_eq!(terminal_display_width(&fitted), 5);
}

#[cfg(unix)]
#[test]
fn raw_picker_query_line_keeps_end_cursor_visible_when_narrow() {
    let query = RawPickerSearchQuery::from_text("abcdef");

    let line = raw_picker_query_prompt_line("skills> ", &query, 12);

    assert_eq!(line, "skills> def\x1b[7m \x1b[0m");
}

#[cfg(unix)]
#[test]
fn raw_picker_query_line_keeps_wide_end_cursor_visible_when_narrow() {
    let query = RawPickerSearchQuery::from_text("模型abc");

    let line = raw_picker_query_prompt_line("skills> ", &query, 12);

    assert_eq!(line, "skills> abc\x1b[7m \x1b[0m");
    assert_eq!(terminal_display_width("skills> abc "), 12);
}

#[cfg(unix)]
#[test]
fn raw_picker_query_line_highlights_emoji_grapheme_at_cursor() {
    let family = "👨‍👩‍👧‍👦";
    let mut query = RawPickerSearchQuery::from_text(&format!("{family}a"));
    query.handle_key(&PromptKeyEvent::new("home", ""));

    let line = raw_picker_query_prompt_line("skills> ", &query, 20);

    assert_eq!(line, format!("skills> \x1b[7m{family}\x1b[0ma"));
}

#[cfg(unix)]
#[test]
fn raw_skills_picker_truncates_rendered_lines_to_terminal_width() {
    let pty = PseudoTerminal::open();
    pty.set_size(24, 20);
    let state = SkillsPickerState::new(
        vec![SkillManagementItem::new(
            "iac-aliyun-超级长技能名称",
            "中文描述中文描述中文描述中文描述",
            SkillManagementSource::Project,
            1200,
            "/very/long/path/中文/skill",
            true,
            false,
        )],
        10,
    );

    let query = RawPickerSearchQuery::from_text("中文查询中文查询");
    let line_count = render_raw_skills_picker(pty.slave, 0, &query, &state)
        .expect("skills picker should render");
    let output = read_fd_until_contains(pty.master, b"...");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);

    assert!(line_count >= visible_lines.len());
    assert!(
        visible_lines
            .iter()
            .all(|line| terminal_display_width(line) <= 20),
        "raw picker wrote a line wider than the terminal: {visible_lines:?}"
    );
    assert!(
        visible_lines.iter().any(|line| line.contains("...")),
        "long picker lines should be visibly truncated: {visible_lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_skills_picker_renders_python_style_chinese_selector() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
    ]);
    let pty = PseudoTerminal::open();
    pty.set_size(24, 160);
    let state = SkillsPickerState::new(
        vec![
            SkillManagementItem::new(
                "iac-aliyun",
                "阿里云 Alibaba Cloud ROS/Terraform IaC 模板生成、解释、完善、校验、询价与部署",
                SkillManagementSource::Bundled,
                7200,
                "",
                true,
                true,
            ),
            SkillManagementItem::new(
                "simplify",
                "Review changed code for reuse, quality, and efficiency, then fix issues found.",
                SkillManagementSource::Bundled,
                436,
                "",
                true,
                true,
            ),
        ],
        10,
    );

    let query = RawPickerSearchQuery::new();
    let line_count = render_raw_skills_picker(pty.slave, 0, &query, &state)
        .expect("skills picker should render");
    let output = read_fd_until_contains(pty.master, "约 109 个 token".as_bytes());
    let visible_lines = raw_visible_lines_from_terminal_output(&output);

    assert_eq!(line_count, 8, "{visible_lines:?}");
    assert!(visible_lines.iter().any(|line| line == "技能 (1 / 2)"));
    assert!(visible_lines
        .iter()
        .any(|line| line == "2 个技能 - 空格切换，Enter 保存，Tab 排序，Esc 取消"));
    assert!(visible_lines.iter().any(|line| line == "排序：名称"));
    assert!(visible_lines.iter().any(|line| line == "> 搜索技能..."));
    assert!(visible_lines.iter().any(|line| {
        line.contains("> - 启用 iac-aliyun")
            && line.contains("内置")
            && line.contains("已锁定")
            && line.contains("约 1.8k 个 token")
    }));
    assert!(visible_lines.iter().any(|line| {
        line.contains("  - 启用 simplify")
            && line.contains("内置")
            && line.contains("已锁定")
            && line.contains("约 109 个 token")
    }));
    assert!(
        visible_lines.iter().all(|line| !line.contains("skills>")
            && !line.contains("Skills (")
            && !line.contains("↑↓ 导航")),
        "{visible_lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_skills_picker_search_filters_description_and_renders_python_styles() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    pty.set_size(24, 160);
    let mut state = SkillsPickerState::new(
        vec![
            SkillManagementItem::new(
                "iac-aliyun",
                "Alibaba Cloud ROS/Terraform IaC template generation",
                SkillManagementSource::Bundled,
                7200,
                "",
                true,
                true,
            ),
            SkillManagementItem::new(
                "simplify",
                "Review changed code for reuse, quality, and efficiency.",
                SkillManagementSource::Bundled,
                436,
                "",
                true,
                true,
            ),
        ],
        10,
    );
    let query = RawPickerSearchQuery::from_text("quality");
    state.update_query(query.text());

    let line_count = render_raw_skills_picker(pty.slave, 0, &query, &state)
        .expect("skills picker should render");
    let output = read_fd_until_contains(pty.master, b"matched description");
    let visible_lines = raw_visible_lines_from_terminal_output(&output);

    assert_eq!(line_count, 7, "{visible_lines:?}");
    assert!(visible_lines.iter().any(|line| line == "Skills (1 of 1)"));
    assert!(visible_lines.iter().any(|line| {
        line == "2 skills - Space to toggle, Enter to save, Tab to sort, Esc to cancel"
    }));
    assert!(visible_lines
        .iter()
        .any(|line| line.contains("> - on simplify") && line.contains("matched description")));
    assert!(
        visible_lines
            .iter()
            .all(|line| !line.contains("iac-aliyun")),
        "{visible_lines:?}"
    );
    assert_bytes_contains(&output, b"\x1b[1m\x1b[36m");
    assert_bytes_contains(&output, b"\x1b[32m");
    assert_bytes_contains(&output, b"\x1b[2m");
}

#[cfg(unix)]
#[test]
fn raw_skills_picker_restores_cursor_to_search_box() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    pty.set_size(24, 160);
    let mut state = SkillsPickerState::new(
        vec![SkillManagementItem::new(
            "iac-aliyun",
            "Alibaba Cloud template generation",
            SkillManagementSource::Bundled,
            7200,
            "",
            true,
            true,
        )],
        10,
    );
    let query = RawPickerSearchQuery::from_text("iac");
    state.update_query(query.text());

    render_raw_skills_picker(pty.slave, 0, &query, &state).expect("skills picker should render");
    let output = read_fd_until_contains(pty.master, b"\x1b[u");

    assert_bytes_contains(&output, b"iac\x1b[s\x1b[7m \x1b[0m");
    assert!(
        output.ends_with(b"\x1b[u"),
        "{:?}",
        String::from_utf8_lossy(&output)
    );
}

#[cfg(unix)]
#[test]
fn raw_skills_picker_clears_previous_results_from_search_cursor() {
    let _env = english_locale_guard();
    let pty = PseudoTerminal::open();
    pty.set_size(24, 160);
    let mut state = SkillsPickerState::new(
        vec![
            SkillManagementItem::new(
                "iac-aliyun",
                "Alibaba Cloud template generation",
                SkillManagementSource::Bundled,
                7200,
                "",
                true,
                true,
            ),
            SkillManagementItem::new(
                "simplify",
                "Review changed code for reuse and quality.",
                SkillManagementSource::Bundled,
                436,
                "",
                true,
                true,
            ),
        ],
        10,
    );

    let query = RawPickerSearchQuery::new();
    let previous_lines = render_raw_skills_picker(pty.slave, 0, &query, &state)
        .expect("skills picker should render");
    let first_output = read_fd_until_contains(pty.master, b"\x1b[u");

    let query = RawPickerSearchQuery::from_text("asdf");
    state.update_query(query.text());
    render_raw_skills_picker(pty.slave, previous_lines, &query, &state)
        .expect("skills picker should rerender");
    let second_output = read_fd_until_contains(pty.master, b"\x1b[u");

    let screen_offset = b"\n\n\n\n\n\n\n\n\n\n";
    let screen =
        raw_ansi_screen_after_writes(160, 24, &[screen_offset, &first_output, &second_output]);

    assert!(
        screen
            .lines
            .iter()
            .any(|line| line.contains("No skills found")),
        "{screen:?}"
    );
    assert!(
        screen
            .lines
            .iter()
            .all(|line| !line.contains("iac-aliyun") && !line.contains("simplify")),
        "{screen:?}"
    );
}

#[cfg(unix)]
#[test]
fn raw_skill_management_item_counts_unicode_characters_like_python() {
    let _env = english_locale_guard();
    let content = "阿".repeat(4000);
    let skill = SkillDefinition {
        name: "iac-aliyun".to_owned(),
        description: "desc".to_owned(),
        allowed_tools: Vec::new(),
        when_to_use: String::new(),
        arguments: Vec::new(),
        content,
        source: SkillSource::Bundled,
        file_path: String::new(),
        skill_root: String::new(),
        user_invocable: true,
        model_override: "inherit".to_owned(),
        effort_override: String::new(),
        context: "inline".to_owned(),
        agent: "general-purpose".to_owned(),
    };

    let item = skill_management_item(&skill, &BTreeSet::new());

    assert_eq!(item.content_length, 4000);
    assert_eq!(
        format_skill_token_estimate(item.content_length),
        "~1.0k tokens"
    );
}

#[cfg(unix)]
#[test]
fn raw_memory_dialog_renders_python_style_memory_entries() {
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "zh_CN.UTF-8"),
        ("LC_ALL", "zh_CN.UTF-8"),
        ("LC_MESSAGES", "zh_CN.UTF-8"),
        ("LANG", "zh_CN.UTF-8"),
        ("IAC_CODE_INSTRUCTION_MEMORY_FILE", "IAC-CODE.md"),
    ]);
    let root = unique_temp_dir("iac-code-rs-memory-dialog");
    let config_dir = root.join("config");
    let cwd = root.join("workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&cwd).expect("workspace dir should be created");
    let paths = paths_for(&config_dir);
    let runtime = memory_runtime_paths(&paths, cwd.to_string_lossy().as_ref());
    let (output, line_count) = raw_memory_dialog_render_output(0, &runtime, true, 0, 4096);
    let project_root =
        find_git_worktree_root(cwd.to_string_lossy().as_ref()).unwrap_or_else(|| cwd.clone());

    assert_eq!(line_count, 9);
    assert!(output.contains("记忆"), "{output:?}");
    assert!(output.contains("❯ 自动记忆：启用"), "{output:?}");
    assert!(
        output.contains(&format!(
            "1. 项目记忆                 保存在 {}",
            project_root.join("IAC-CODE.md").display()
        )),
        "{output:?}"
    );
    assert!(
        output.contains(&format!(
            "2. 用户记忆                 保存在 {}",
            config_dir.join("IAC-CODE.md").display()
        )),
        "{output:?}"
    );
    assert!(
        output.contains(&format!(
            "3. 打开自动记忆文件夹       {}",
            config_dir
                .join("projects")
                .join(sanitize_path(project_root.to_string_lossy().as_ref()))
                .join("memory")
                .display()
        )),
        "{output:?}"
    );
    assert!(output.contains("Enter 确认 · Esc 取消"), "{output:?}");
    fs::remove_dir_all(root).ok();
}
