use std::collections::BTreeMap;

use iac_code_config::i18n::detect_language;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct SkillFrontmatter {
    pub(super) name: String,
    pub(super) description: String,
    descriptions: BTreeMap<String, String>,
    pub(super) allowed_tools: Vec<String>,
    pub(super) when_to_use: String,
    pub(super) arguments: Vec<String>,
    pub(super) user_invocable: bool,
    pub(super) model: String,
    pub(super) effort: String,
    pub(super) context: String,
    pub(super) agent: String,
}

impl Default for SkillFrontmatter {
    fn default() -> Self {
        Self {
            name: String::new(),
            description: String::new(),
            descriptions: BTreeMap::new(),
            allowed_tools: Vec::new(),
            when_to_use: String::new(),
            arguments: Vec::new(),
            user_invocable: true,
            model: "inherit".into(),
            effort: String::new(),
            context: "inline".into(),
            agent: "general-purpose".into(),
        }
    }
}

pub(super) fn parse_frontmatter(markdown: &str) -> (SkillFrontmatter, String) {
    if !markdown.starts_with("---") {
        return (SkillFrontmatter::default(), markdown.to_owned());
    }

    let mut lines = markdown.lines();
    if lines.next() != Some("---") {
        return (SkillFrontmatter::default(), markdown.to_owned());
    }

    let mut frontmatter_lines = Vec::new();
    let mut body_lines = Vec::new();
    let mut in_frontmatter = true;
    for line in lines {
        if in_frontmatter && line.trim() == "---" {
            in_frontmatter = false;
            continue;
        }
        if in_frontmatter {
            frontmatter_lines.push(line);
        } else {
            body_lines.push(line);
        }
    }

    if in_frontmatter {
        return (SkillFrontmatter::default(), markdown.to_owned());
    }

    (
        parse_skill_frontmatter(&frontmatter_lines.join("\n")),
        body_lines.join("\n"),
    )
}

fn parse_skill_frontmatter(text: &str) -> SkillFrontmatter {
    let mut frontmatter = SkillFrontmatter::default();
    let mut current_list_key: Option<String> = None;
    let mut current_list_indent = 0usize;
    let mut current_map_key: Option<String> = None;
    let mut current_map_indent = 0usize;

    for raw_line in text.lines() {
        let line = raw_line.trim_end();
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let indent = line.len() - trimmed.len();

        if let Some(key) = current_list_key.clone() {
            if let Some(value) = trimmed.strip_prefix("- ") {
                if indent > current_list_indent {
                    push_frontmatter_list_value(&mut frontmatter, &key, unquote_yaml(value.trim()));
                    continue;
                }
            }
            current_list_key = None;
        }

        if let Some(key) = current_map_key.clone() {
            if indent > current_map_indent {
                if let Some((nested_key, value)) = split_yaml_key_value(trimmed) {
                    push_frontmatter_map_value(
                        &mut frontmatter,
                        &key,
                        nested_key,
                        unquote_yaml(value.trim()),
                    );
                    continue;
                }
            }
            current_map_key = None;
        }

        let Some((key, value)) = split_yaml_key_value(trimmed) else {
            continue;
        };
        match key.as_str() {
            "name" => frontmatter.name = unquote_yaml(&value),
            "description" => frontmatter.description = unquote_yaml(&value),
            "when_to_use" => frontmatter.when_to_use = unquote_yaml(&value),
            "user_invocable" => frontmatter.user_invocable = parse_yaml_bool(&value, true),
            "model" => frontmatter.model = unquote_yaml(&value),
            "effort" => frontmatter.effort = unquote_yaml(&value),
            "context" => frontmatter.context = unquote_yaml(&value),
            "agent" => frontmatter.agent = unquote_yaml(&value),
            "allowed_tools" | "arguments" => {
                let values = parse_inline_string_list(&value);
                if values.is_empty() && value.trim().is_empty() {
                    current_list_key = Some(key);
                    current_list_indent = indent;
                } else {
                    for value in values {
                        push_frontmatter_list_value(&mut frontmatter, &key, value);
                    }
                }
            }
            "descriptions" => {
                let values = parse_inline_string_map(&value);
                if values.is_empty() && value.trim().is_empty() {
                    current_map_key = Some(key);
                    current_map_indent = indent;
                } else {
                    for (language, description) in values {
                        push_frontmatter_map_value(&mut frontmatter, &key, language, description);
                    }
                }
            }
            _ => {}
        }
    }

    if let Some(description) = frontmatter
        .descriptions
        .get(&detect_language())
        .filter(|description| !description.is_empty())
    {
        frontmatter.description = description.clone();
    }

    frontmatter
}

fn push_frontmatter_list_value(frontmatter: &mut SkillFrontmatter, key: &str, value: String) {
    if value.is_empty() {
        return;
    }
    match key {
        "allowed_tools" => frontmatter.allowed_tools.push(value),
        "arguments" => frontmatter.arguments.push(value),
        _ => {}
    }
}

fn push_frontmatter_map_value(
    frontmatter: &mut SkillFrontmatter,
    key: &str,
    nested_key: String,
    value: String,
) {
    if nested_key.is_empty() || value.is_empty() {
        return;
    }
    if key == "descriptions" {
        frontmatter.descriptions.insert(nested_key, value);
    }
}

fn split_yaml_key_value(line: &str) -> Option<(String, String)> {
    let (key, value) = line.split_once(':')?;
    Some((unquote_yaml(key.trim()), value.trim().to_owned()))
}

fn parse_inline_string_list(value: &str) -> Vec<String> {
    let trimmed = value.trim();
    if !(trimmed.starts_with('[') && trimmed.ends_with(']')) {
        return Vec::new();
    }
    trimmed[1..trimmed.len() - 1]
        .split(',')
        .map(str::trim)
        .map(unquote_yaml)
        .filter(|item| !item.is_empty())
        .collect()
}

fn parse_inline_string_map(value: &str) -> BTreeMap<String, String> {
    let trimmed = value.trim();
    if !(trimmed.starts_with('{') && trimmed.ends_with('}')) {
        return BTreeMap::new();
    }
    let mut values = BTreeMap::new();
    for item in trimmed[1..trimmed.len() - 1].split(',') {
        let Some((key, value)) = split_yaml_key_value(item.trim()) else {
            continue;
        };
        let value = unquote_yaml(value.trim());
        if !key.is_empty() && !value.is_empty() {
            values.insert(key, value);
        }
    }
    values
}

fn unquote_yaml(value: &str) -> String {
    let bytes = value.as_bytes();
    if bytes.len() >= 2
        && ((bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"')
            || (bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\''))
    {
        return value[1..value.len() - 1]
            .replace("\\\"", "\"")
            .replace("\\\\", "\\");
    }
    value.to_owned()
}

fn parse_yaml_bool(value: &str, default: bool) -> bool {
    match unquote_yaml(value).trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" | "1" => true,
        "false" | "no" | "off" | "0" => false,
        _ => default,
    }
}
