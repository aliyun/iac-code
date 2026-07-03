pub(super) fn upsert_root_string_setting(content: &str, key: &str, value: &str) -> String {
    let mut content = remove_root_yaml_block(content, key);
    if !content.is_empty() && !content.ends_with('\n') {
        content.push('\n');
    }
    content.push_str(key);
    content.push_str(": ");
    content.push_str(&quote_yaml_scalar(value));
    content.push('\n');
    content
}

pub(super) fn upsert_provider_string_setting(
    content: &str,
    provider_key: &str,
    field: &str,
    value: &str,
) -> String {
    let mut lines = content
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect::<Vec<_>>();
    let value_line = format!("{field}: {}", quote_yaml_scalar(value));

    let Some(providers_index) = find_root_key_line(&lines, "providers") else {
        if !lines.is_empty() {
            lines.push(String::new());
        }
        lines.push("providers:".to_owned());
        lines.push(format!("  {provider_key}:"));
        lines.push(format!("    {value_line}"));
        return yaml_lines_to_string(lines);
    };

    let providers_indent = yaml_indent(&lines[providers_index]);
    let providers_end = yaml_block_end(&lines, providers_index + 1, providers_indent);
    let provider_index =
        find_child_key_line(&lines, providers_index + 1, providers_end, provider_key);

    let Some(provider_index) = provider_index else {
        lines.insert(providers_end, format!("  {provider_key}:"));
        lines.insert(providers_end + 1, format!("    {value_line}"));
        return yaml_lines_to_string(lines);
    };

    let provider_indent = yaml_indent(&lines[provider_index]);
    let provider_end = yaml_block_end(&lines, provider_index + 1, provider_indent);
    if let Some(field_index) = find_child_key_line(&lines, provider_index + 1, provider_end, field)
    {
        lines[field_index] = format!(
            "{}{}",
            " ".repeat(yaml_indent(&lines[field_index])),
            value_line
        );
    } else {
        lines.insert(
            provider_end,
            format!("{}{}", " ".repeat(provider_indent + 2), value_line),
        );
    }
    yaml_lines_to_string(lines)
}

pub(super) fn remove_root_yaml_block(content: &str, key: &str) -> String {
    let mut output = Vec::new();
    let mut skipping = false;
    let mut block_indent = 0_usize;

    for line in content.lines() {
        let trimmed_end = line.trim_end();
        let trimmed_start = trimmed_end.trim_start();
        let indent = trimmed_end.len() - trimmed_start.len();

        if skipping {
            if !trimmed_start.is_empty()
                && indent <= block_indent
                && !trimmed_start.starts_with('-')
            {
                skipping = false;
            } else {
                continue;
            }
        }

        if !skipping {
            let is_target = indent == 0
                && trimmed_start
                    .split_once(':')
                    .is_some_and(|(candidate, _)| candidate.trim() == key);
            if is_target {
                skipping = true;
                block_indent = indent;
                continue;
            }
            output.push(trimmed_end.to_owned());
        }
    }

    while output.last().is_some_and(|line| line.trim().is_empty()) {
        output.pop();
    }
    if output.is_empty() {
        String::new()
    } else {
        let mut text = output.join("\n");
        text.push('\n');
        text
    }
}

pub(super) fn quote_yaml_scalar(value: &str) -> String {
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.'))
    {
        return value.to_owned();
    }
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

fn find_root_key_line(lines: &[String], key: &str) -> Option<usize> {
    lines.iter().enumerate().find_map(|(index, line)| {
        (yaml_indent(line) == 0 && yaml_key(line).is_some_and(|candidate| candidate == key))
            .then_some(index)
    })
}

fn find_child_key_line(lines: &[String], start: usize, end: usize, key: &str) -> Option<usize> {
    lines[start..end]
        .iter()
        .enumerate()
        .find_map(|(offset, line)| {
            (yaml_key(line).is_some_and(|candidate| candidate == key)).then_some(start + offset)
        })
}

fn yaml_block_end(lines: &[String], start: usize, parent_indent: usize) -> usize {
    let mut index = start;
    while index < lines.len() {
        let trimmed = lines[index].trim_start();
        if !trimmed.is_empty() && yaml_indent(&lines[index]) <= parent_indent {
            break;
        }
        index += 1;
    }
    index
}

fn yaml_key(line: &str) -> Option<&str> {
    let trimmed = line.trim_start();
    if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with('-') {
        return None;
    }
    trimmed.split_once(':').map(|(key, _)| key.trim())
}

fn yaml_indent(line: &str) -> usize {
    line.len() - line.trim_start().len()
}

fn yaml_lines_to_string(mut lines: Vec<String>) -> String {
    while lines.last().is_some_and(|line| line.trim().is_empty()) {
        lines.pop();
    }
    if lines.is_empty() {
        String::new()
    } else {
        let mut content = lines.join("\n");
        content.push('\n');
        content
    }
}
