use std::collections::BTreeMap;
use std::time::Instant;

use iac_code_protocol::json::JsonValue;

const SUBTITLE_MAX_LEN: usize = 60;

#[derive(Clone, Debug)]
pub struct ToolCallState {
    pub tool_call_id: String,
    pub tool_name: String,
    pub accumulated_input: String,
    pub title: String,
    start_time: Instant,
}

impl ToolCallState {
    pub fn new(tool_call_id: impl Into<String>, tool_name: impl Into<String>) -> Self {
        let tool_name = tool_name.into();
        Self {
            tool_call_id: tool_call_id.into(),
            accumulated_input: String::new(),
            title: tool_name.clone(),
            tool_name,
            start_time: Instant::now(),
        }
    }

    pub fn elapsed_ms(&self) -> u128 {
        self.start_time.elapsed().as_millis()
    }

    pub fn update_input(&mut self, delta: &str) {
        self.accumulated_input.push_str(delta);
        self.update_title();
    }

    fn update_title(&mut self) {
        let subtitle = extract_key_argument(&self.tool_name, &self.accumulated_input);
        self.title = if subtitle.is_empty() {
            self.tool_name.clone()
        } else {
            format!("{}: {}", self.tool_name, subtitle)
        };
    }
}

#[derive(Clone, Debug, Default)]
pub struct TurnState {
    pub turn_id: String,
    pub tool_calls: BTreeMap<String, ToolCallState>,
}

impl TurnState {
    pub fn new(turn_id: impl Into<String>) -> Self {
        Self {
            turn_id: turn_id.into(),
            tool_calls: BTreeMap::new(),
        }
    }

    pub fn start_tool_call(&mut self, tool_call_id: &str, tool_name: &str) -> &ToolCallState {
        self.tool_calls.insert(
            tool_call_id.to_owned(),
            ToolCallState::new(tool_call_id, tool_name),
        );
        self.tool_calls
            .get(tool_call_id)
            .expect("tool call was just inserted")
    }

    pub fn get_tool_call(&self, tool_call_id: &str) -> Option<&ToolCallState> {
        self.tool_calls.get(tool_call_id)
    }

    pub fn get_tool_call_mut(&mut self, tool_call_id: &str) -> Option<&mut ToolCallState> {
        self.tool_calls.get_mut(tool_call_id)
    }
}

pub fn extract_key_argument(tool_name: &str, raw_json: &str) -> String {
    let Some(key) = key_argument(tool_name) else {
        return String::new();
    };
    if let Ok(JsonValue::Object(fields)) = iac_code_protocol::json::parse(raw_json) {
        if let Some(JsonValue::String(value)) = fields.get(key) {
            return truncate_chars(value, SUBTITLE_MAX_LEN);
        }
        return String::new();
    }

    let marker = format!("\"{key}\"");
    let Some(index) = raw_json.find(&marker) else {
        return String::new();
    };
    let rest = raw_json[index + marker.len()..].trim_start_matches([':', ' ']);
    let Some(rest) = rest.strip_prefix('"') else {
        return String::new();
    };
    let end = rest.find('"').unwrap_or(rest.len());
    truncate_chars(&rest[..end], SUBTITLE_MAX_LEN)
}

fn key_argument(tool_name: &str) -> Option<&'static str> {
    match tool_name {
        "bash" => Some("command"),
        "read_file" | "write_file" | "edit_file" => Some("file_path"),
        "glob" | "grep" => Some("pattern"),
        "list_files" => Some("path"),
        "web_fetch" => Some("url"),
        _ => None,
    }
}

fn truncate_chars(value: &str, max_len: usize) -> String {
    value.chars().take(max_len).collect()
}
