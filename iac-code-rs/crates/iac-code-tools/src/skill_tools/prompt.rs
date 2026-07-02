use std::process::Command;

use crate::ToolContext;

use super::SkillDefinition;

pub(super) fn render_skill_prompt(
    skill: &SkillDefinition,
    args: &str,
    context: &ToolContext,
) -> String {
    let mut segments = Vec::new();
    if !skill.skill_root.is_empty() {
        segments.push(PromptSegment::Text(format!(
            "Base directory for this skill: {}\n\n",
            skill.skill_root
        )));
    }
    segments.extend(parse_prompt_segments(&skill.content));
    render_prompt_segments(&segments, skill, args, context)
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum PromptSegment {
    Text(String),
    InlineShell(String),
    BlockShell(String),
}

fn render_prompt_segments(
    segments: &[PromptSegment],
    skill: &SkillDefinition,
    args: &str,
    context: &ToolContext,
) -> String {
    let mut rendered_parts = Vec::new();
    let mut text_placeholder_used = false;
    for segment in segments {
        match segment {
            PromptSegment::Text(text) => {
                let (rendered_text, used) = render_text_segment(text, skill, args, &context.cwd);
                text_placeholder_used |= used;
                rendered_parts.push(rendered_text);
            }
            PromptSegment::InlineShell(command) => {
                let command = render_builtin_variables(command, skill, &context.cwd);
                rendered_parts.push(run_shell(&command, &context.cwd).trim().to_owned());
            }
            PromptSegment::BlockShell(command) => {
                let command = render_builtin_variables(command, skill, &context.cwd);
                rendered_parts.push(run_shell(&command, &context.cwd));
            }
        }
    }

    let mut rendered = rendered_parts.join("");
    if args.is_empty() {
        return rendered;
    }

    if !text_placeholder_used {
        rendered.push_str("\n\nARGUMENTS: ");
        rendered.push_str(args);
    }
    rendered
}

fn render_text_segment(
    text: &str,
    skill: &SkillDefinition,
    args: &str,
    cwd: &str,
) -> (String, bool) {
    let (rendered, used) = substitute_arguments(text, args, &skill.arguments);
    (render_builtin_variables(&rendered, skill, cwd), used)
}

fn render_builtin_variables(content: &str, skill: &SkillDefinition, cwd: &str) -> String {
    content
        .replace("${SKILL_DIR}", &skill.skill_root)
        .replace("${SESSION_ID}", "")
        .replace("${CWD}", cwd)
}

fn substitute_arguments(content: &str, args: &str, argument_names: &[String]) -> (String, bool) {
    if args.is_empty() {
        return (content.to_owned(), false);
    }

    let original = content.to_owned();
    let parsed_args = parse_arguments(args);
    let mut rendered = content.to_owned();
    for (index, name) in argument_names.iter().enumerate() {
        if let Some(value) = parsed_args.get(index) {
            rendered = replace_named_argument(&rendered, name, value);
        }
    }
    rendered = replace_indexed_arguments(&rendered, &parsed_args);
    rendered = replace_short_indexed_arguments(&rendered, &parsed_args);
    rendered = rendered.replace("$ARGUMENTS", args);
    let used = rendered != original;
    (rendered, used)
}

fn replace_named_argument(content: &str, name: &str, value: &str) -> String {
    if name.is_empty() {
        return content.to_owned();
    }
    let needle = format!("${name}");
    let mut output = String::new();
    let mut rest = content;
    while let Some(index) = rest.find(&needle) {
        output.push_str(&rest[..index]);
        let after = &rest[index + needle.len()..];
        let next = after.chars().next();
        if next.is_none_or(|ch| ch != '[' && !is_ascii_word_char(ch)) {
            output.push_str(value);
        } else {
            output.push_str(&needle);
        }
        rest = after;
    }
    output.push_str(rest);
    output
}

fn replace_indexed_arguments(content: &str, args: &[String]) -> String {
    let mut output = String::new();
    let mut rest = content;
    const PREFIX: &str = "$ARGUMENTS[";
    while let Some(index) = rest.find(PREFIX) {
        output.push_str(&rest[..index]);
        let after_prefix = &rest[index + PREFIX.len()..];
        let digit_len = after_prefix
            .chars()
            .take_while(|ch| ch.is_ascii_digit())
            .map(char::len_utf8)
            .sum::<usize>();
        if digit_len == 0 || !after_prefix[digit_len..].starts_with(']') {
            output.push_str(PREFIX);
            rest = after_prefix;
            continue;
        }
        let argument_index = after_prefix[..digit_len]
            .parse::<usize>()
            .unwrap_or(usize::MAX);
        output.push_str(args.get(argument_index).map(String::as_str).unwrap_or(""));
        rest = &after_prefix[digit_len + 1..];
    }
    output.push_str(rest);
    output
}

fn replace_short_indexed_arguments(content: &str, args: &[String]) -> String {
    let mut output = String::new();
    let mut chars = content.char_indices().peekable();
    while let Some((_, ch)) = chars.next() {
        if ch != '$' {
            output.push(ch);
            continue;
        }

        let mut digits = String::new();
        while let Some((_, next)) = chars.peek().copied() {
            if !next.is_ascii_digit() {
                break;
            }
            digits.push(next);
            chars.next();
        }
        if digits.is_empty() {
            output.push('$');
            continue;
        }
        if chars
            .peek()
            .map(|(_, next)| is_ascii_word_char(*next))
            .unwrap_or(false)
        {
            output.push('$');
            output.push_str(&digits);
            continue;
        }
        let argument_index = digits.parse::<usize>().unwrap_or(usize::MAX);
        output.push_str(args.get(argument_index).map(String::as_str).unwrap_or(""));
    }
    output
}

fn parse_arguments(args: &str) -> Vec<String> {
    let mut parsed = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    for ch in args.chars() {
        if let Some(quote_ch) = quote {
            if ch == quote_ch {
                quote = None;
            } else {
                current.push(ch);
            }
            continue;
        }
        if ch == '\'' || ch == '"' {
            quote = Some(ch);
            continue;
        }
        if ch.is_whitespace() {
            if !current.is_empty() {
                parsed.push(std::mem::take(&mut current));
            }
            continue;
        }
        current.push(ch);
    }
    if quote.is_some() {
        return args.split_whitespace().map(str::to_owned).collect();
    }
    if !current.is_empty() {
        parsed.push(current);
    }
    parsed
}

fn parse_prompt_segments(content: &str) -> Vec<PromptSegment> {
    let mut segments = Vec::new();
    let mut rest = content;
    while !rest.is_empty() {
        let next_block = rest.find("```!");
        let next_inline = rest.find("!`");
        match (next_block, next_inline) {
            (None, None) => {
                segments.push(PromptSegment::Text(rest.to_owned()));
                break;
            }
            (Some(block), Some(inline)) if inline < block => {
                push_inline_shell_segment(&mut segments, &mut rest, inline);
            }
            (None, Some(inline)) => {
                push_inline_shell_segment(&mut segments, &mut rest, inline);
            }
            (Some(block), _) => {
                push_block_shell_segment(&mut segments, &mut rest, block);
            }
        }
    }
    segments
}

fn push_inline_shell_segment(segments: &mut Vec<PromptSegment>, rest: &mut &str, start: usize) {
    if start > 0 {
        segments.push(PromptSegment::Text(rest[..start].to_owned()));
    }
    let after_marker = &rest[start + "!`".len()..];
    let Some(end) = after_marker.find('`') else {
        segments.push(PromptSegment::Text(rest[start..].to_owned()));
        *rest = "";
        return;
    };
    segments.push(PromptSegment::InlineShell(
        after_marker[..end].trim().to_owned(),
    ));
    *rest = &after_marker[end + 1..];
}

fn push_block_shell_segment(segments: &mut Vec<PromptSegment>, rest: &mut &str, start: usize) {
    if start > 0 {
        segments.push(PromptSegment::Text(rest[..start].to_owned()));
    }
    let after_marker = &rest[start + "```!".len()..];
    let Some(end) = after_marker.find("```") else {
        segments.push(PromptSegment::Text(rest[start..].to_owned()));
        *rest = "";
        return;
    };
    segments.push(PromptSegment::BlockShell(
        after_marker[..end].trim().to_owned(),
    ));
    *rest = &after_marker[end + "```".len()..];
}

pub(super) fn contains_shell_commands(content: &str) -> bool {
    content.contains("```!") || content.contains("!`")
}

fn run_shell(command: &str, cwd: &str) -> String {
    let mut process = Command::new("/bin/sh");
    process.arg("-c").arg(command);
    if !cwd.is_empty() {
        process.current_dir(cwd);
    }
    match process.output() {
        Ok(output) => String::from_utf8_lossy(&output.stdout).into_owned(),
        Err(error) => format!("[shell error: {error}]"),
    }
}

fn is_ascii_word_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}
