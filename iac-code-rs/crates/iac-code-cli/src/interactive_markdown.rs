use iac_code_tui::render_markdown_ansi;

use crate::interactive_banner::interactive_startup_banner_width;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum InteractiveMarkdownPrefix {
    Bullet,
    Continuation,
    None,
}

pub(super) fn render_interactive_agent_stdout(stdout: &str) -> String {
    let Some(markdown) = stdout.strip_suffix('\n') else {
        return render_markdown_ansi(stdout, Some(interactive_startup_banner_width()));
    };
    let mut rendered = render_markdown_ansi(markdown, Some(interactive_startup_banner_width()));
    rendered.push('\n');
    rendered
}

pub(super) fn prefix_interactive_markdown_block(
    rendered: &str,
    prefix: InteractiveMarkdownPrefix,
) -> String {
    let mut output = String::new();
    for (index, line) in rendered.lines().enumerate() {
        match prefix {
            InteractiveMarkdownPrefix::Bullet if index == 0 => output.push_str("✦ "),
            InteractiveMarkdownPrefix::Bullet | InteractiveMarkdownPrefix::Continuation => {
                output.push_str("  ")
            }
            InteractiveMarkdownPrefix::None => {}
        }
        output.push_str(line);
        output.push('\n');
    }
    output
}

pub(super) fn markdown_source_starts_with_heading(text: &str) -> bool {
    text.lines().find_map(|line| {
        let trimmed = line.trim_start();
        if trimmed.is_empty() {
            None
        } else {
            Some(markdown_heading_like(trimmed))
        }
    }) == Some(true)
}

fn markdown_heading_like(line: &str) -> bool {
    let hashes = line
        .chars()
        .take_while(|character| *character == '#')
        .count();
    (1..=6).contains(&hashes) && line.chars().nth(hashes).is_some_and(char::is_whitespace)
}

pub(super) fn streaming_markdown_flush_index(text: &str) -> Option<usize> {
    if text.is_empty() {
        return None;
    }
    if !text_needs_markdown_context(text) {
        return text.rfind('\n').map(|index| index + 1);
    }
    if has_pending_markdown_table(text) {
        return None;
    }
    find_safe_markdown_split(text)
}

fn text_needs_markdown_context(text: &str) -> bool {
    text.lines().any(|line| {
        let trimmed = line.trim_start();
        trimmed.starts_with('#')
            || trimmed.starts_with('|')
            || trimmed.starts_with("```")
            || trimmed.starts_with("- ")
            || trimmed.starts_with("* ")
            || trimmed.starts_with("+ ")
            || ordered_list_item_like(trimmed)
    }) || text.contains("**")
        || text.contains('`')
}

fn ordered_list_item_like(line: &str) -> bool {
    let Some((number, _)) = line.split_once(". ") else {
        return false;
    };
    !number.is_empty() && number.chars().all(|character| character.is_ascii_digit())
}

fn has_pending_markdown_table(text: &str) -> bool {
    let start = text.rfind("\n\n").map(|index| index + 2).unwrap_or(0);
    let tail = &text[start..];
    tail.lines().any(|line| line.trim_start().starts_with('|')) && !tail.contains("\n\n")
}

fn find_safe_markdown_split(text: &str) -> Option<usize> {
    let mut in_fence = false;
    let mut last_safe = None;
    let mut index = 0usize;
    while index < text.len() {
        let rest = &text[index..];
        if rest.starts_with("```") {
            in_fence = !in_fence;
            index += 3;
            while index < text.len() {
                let Some(character) = text[index..].chars().next() else {
                    break;
                };
                if character == '\n' {
                    break;
                }
                index += character.len_utf8();
            }
            continue;
        }
        if !in_fence && rest.starts_with("\n\n") {
            last_safe = Some(index + 2);
            index += 2;
            continue;
        }
        let Some(character) = rest.chars().next() else {
            break;
        };
        index += character.len_utf8();
    }
    last_safe
}
