use super::render_markdown_ansi;
use crate::ansi::strip_ansi_sequences;
use crate::width::terminal_display_width;

#[test]
fn renders_common_assistant_markdown_without_raw_markers() {
    let rendered = render_markdown_ansi(
        "## Background\n`iac-code` is **an IaC assistant**.\n\n| Module | Responsibility |\n| --- | --- |\n| cli | Entry point |\n\n- Generates ROS templates",
        Some(120),
    );

    assert!(rendered.contains("Background"), "{rendered:?}");
    assert!(rendered.contains("\x1b[1m"), "{rendered:?}");
    assert!(rendered.contains("\x1b[36m"), "{rendered:?}");
    assert!(rendered.contains("Module"), "{rendered:?}");
    assert!(rendered.contains("Generates ROS templates"), "{rendered:?}");
    assert!(!rendered.contains("## Background"), "{rendered:?}");
    assert!(!rendered.contains("**an IaC assistant**"), "{rendered:?}");
    assert!(
        !rendered.contains("| Module | Responsibility |"),
        "{rendered:?}"
    );
}

#[test]
fn unwraps_markdown_fences_containing_tables_like_codex_agent_renderer() {
    let rendered = render_markdown_ansi(
        "```markdown\n| Module | Responsibility |\n| --- | --- |\n| cli | Entry point |\n```\n",
        Some(120),
    );

    assert!(rendered.contains("Module"), "{rendered:?}");
    assert!(rendered.contains("Responsibility"), "{rendered:?}");
    assert!(rendered.contains("Entry point"), "{rendered:?}");
    assert!(!rendered.contains("```"), "{rendered:?}");
    assert!(!rendered.contains("markdown"), "{rendered:?}");
    assert!(
        !rendered.contains("| Module | Responsibility |"),
        "{rendered:?}"
    );
}

#[test]
fn unwraps_no_outer_pipe_markdown_fence_tables_like_codex_agent_renderer() {
    let rendered = render_markdown_ansi(
        "```md\nModule | Responsibility\n--- | ---\ncli | Entry point\n```\n",
        Some(120),
    );

    assert!(rendered.contains("Module"), "{rendered:?}");
    assert!(rendered.contains("Entry point"), "{rendered:?}");
    assert!(
        !rendered.contains("Module | Responsibility"),
        "{rendered:?}"
    );
}

#[test]
fn renders_ansi_table_cells_with_wide_char_width_alignment() {
    let rendered = render_markdown_ansi(
        "| 名称 | 值 |\n| --- | --- |\n| **阿里** | `qwen` |\n",
        Some(120),
    );

    let visible = rendered
        .lines()
        .map(strip_ansi_sequences)
        .collect::<Vec<_>>();

    assert_eq!(visible[0], "名称   值  ");
    assert_eq!(visible[2], "阿里   qwen");
    assert_eq!(
        terminal_display_width(&visible[0]),
        terminal_display_width(&visible[1])
    );
    assert_eq!(
        terminal_display_width(&visible[1]),
        terminal_display_width(&visible[2])
    );
}

#[test]
fn keeps_markdown_fences_without_tables_as_code_like_codex_agent_renderer() {
    let rendered = render_markdown_ansi("```markdown\n**bold**\n```\n", Some(80));

    assert!(rendered.contains("**bold**"), "{rendered:?}");
    assert!(!rendered.contains("markdown"), "{rendered:?}");
    assert!(!rendered.contains("\x1b[1mbold\x1b[0m"), "{rendered:?}");
}

#[test]
fn leaves_plain_text_plain() {
    assert_eq!(
        render_markdown_ansi("fixture response: hello", Some(80)),
        "fixture response: hello"
    );
}
