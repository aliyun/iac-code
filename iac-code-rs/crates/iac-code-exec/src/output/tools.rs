use iac_code_protocol::json::JsonValue;

use super::json_format::{array_json, bool_json, json_string, json_value_python, object_json};

#[derive(Clone, Debug, Default, PartialEq)]
pub(super) struct ToolUseTracker {
    entries: Vec<ToolUseEntry>,
}

impl ToolUseTracker {
    pub(super) fn record_start(&mut self, tool_use_id: &str, name: String) {
        self.entry_mut(tool_use_id).name = Some(name);
    }

    pub(super) fn record_input(&mut self, tool_use_id: &str, input: JsonValue) {
        self.entry_mut(tool_use_id).input = Some(input);
    }

    pub(super) fn record_result(&mut self, tool_use_id: &str, result: String, is_error: bool) {
        let entry = self.entry_mut(tool_use_id);
        entry.result = Some(result);
        entry.is_error = Some(is_error);
    }

    pub(super) fn to_json(&self) -> String {
        let values = self
            .entries
            .iter()
            .map(|entry| {
                let mut fields = Vec::new();
                if let Some(name) = &entry.name {
                    fields.push(("name", json_string(name)));
                }
                if let Some(input) = &entry.input {
                    fields.push(("input", json_value_python(input)));
                }
                if let Some(result) = &entry.result {
                    fields.push(("result", json_string(result)));
                }
                if let Some(is_error) = entry.is_error {
                    fields.push(("is_error", bool_json(is_error)));
                }
                object_json(&fields)
            })
            .collect::<Vec<_>>();
        array_json(&values)
    }

    fn entry_mut(&mut self, tool_use_id: &str) -> &mut ToolUseEntry {
        if let Some(index) = self
            .entries
            .iter()
            .position(|entry| entry.tool_use_id == tool_use_id)
        {
            return &mut self.entries[index];
        }
        self.entries.push(ToolUseEntry {
            tool_use_id: tool_use_id.to_owned(),
            ..ToolUseEntry::default()
        });
        self.entries.last_mut().expect("tool entry was just pushed")
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
struct ToolUseEntry {
    tool_use_id: String,
    name: Option<String>,
    input: Option<JsonValue>,
    result: Option<String>,
    is_error: Option<bool>,
}
