use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::Path;

use crate::file_security::write_private_file;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum YamlValue {
    String(String),
    Map(BTreeMap<String, YamlValue>),
}

impl YamlValue {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            YamlValue::String(value) => Some(value),
            YamlValue::Map(_) => None,
        }
    }

    pub fn as_map(&self) -> Option<&BTreeMap<String, YamlValue>> {
        match self {
            YamlValue::String(_) => None,
            YamlValue::Map(value) => Some(value),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Entry {
    indent: usize,
    key: String,
    value: Option<String>,
}

pub fn load_yaml_map(path: &Path) -> io::Result<BTreeMap<String, YamlValue>> {
    if !path.exists() {
        return Ok(BTreeMap::new());
    }

    let content = fs::read_to_string(path)?;
    let entries = parse_entries(&content);
    let mut index = 0;
    Ok(parse_map(&entries, &mut index, 0))
}

pub fn string_map(value: Option<&YamlValue>) -> BTreeMap<String, String> {
    value
        .and_then(YamlValue::as_map)
        .map(|items| {
            items
                .iter()
                .filter_map(|(key, value)| {
                    value.as_str().map(|text| (key.clone(), text.to_owned()))
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn save_yaml_map(path: &Path, values: &BTreeMap<String, YamlValue>) -> io::Result<()> {
    let mut content = String::new();
    write_map(values, 0, &mut content);
    write_private_file(path, content)
}

fn parse_entries(content: &str) -> Vec<Entry> {
    content
        .lines()
        .filter_map(|line| {
            let trimmed_end = line.trim_end();
            let trimmed_start = trimmed_end.trim_start();
            if trimmed_start.is_empty() || trimmed_start.starts_with('#') {
                return None;
            }

            let indent = trimmed_end.len() - trimmed_start.len();
            let (key, value) = trimmed_start.split_once(':')?;
            let value = value.trim();
            Some(Entry {
                indent,
                key: unquote(key.trim()),
                value: (!value.is_empty()).then(|| unquote(value)),
            })
        })
        .collect()
}

fn parse_map(entries: &[Entry], index: &mut usize, indent: usize) -> BTreeMap<String, YamlValue> {
    let mut map = BTreeMap::new();

    while let Some(entry) = entries.get(*index) {
        if entry.indent < indent {
            break;
        }
        if entry.indent > indent {
            break;
        }

        *index += 1;
        let value = if let Some(value) = &entry.value {
            YamlValue::String(value.clone())
        } else {
            let child_indent = entries
                .get(*index)
                .filter(|child| child.indent > entry.indent)
                .map(|child| child.indent)
                .unwrap_or(entry.indent + 2);
            YamlValue::Map(parse_map(entries, index, child_indent))
        };
        map.insert(entry.key.clone(), value);
    }

    map
}

fn write_map(values: &BTreeMap<String, YamlValue>, indent: usize, output: &mut String) {
    let prefix = " ".repeat(indent);
    for (key, value) in values {
        match value {
            YamlValue::String(value) => {
                output.push_str(&prefix);
                output.push_str(key);
                output.push_str(": ");
                output.push_str(&quote_if_needed(value));
                output.push('\n');
            }
            YamlValue::Map(values) => {
                output.push_str(&prefix);
                output.push_str(key);
                output.push_str(":\n");
                write_map(values, indent + 2, output);
            }
        }
    }
}

fn quote_if_needed(value: &str) -> String {
    if value.is_empty()
        || value.starts_with(char::is_whitespace)
        || value.ends_with(char::is_whitespace)
        || value.contains([':', '#', '\n', '\r', '"'])
    {
        return format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""));
    }
    value.to_owned()
}

fn unquote(value: &str) -> String {
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
