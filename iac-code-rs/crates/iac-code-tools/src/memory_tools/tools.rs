use std::io;
use std::path::PathBuf;

use iac_code_protocol::json::{self, JsonValue};

use super::manager::MemoryManager;
use super::MEMORY_TYPES;
use crate::{Tool, ToolContext, ToolRegistry, ToolResult};

#[derive(Clone, Debug)]
pub struct ReadMemoryTool {
    manager: MemoryManager,
}

impl ReadMemoryTool {
    pub fn new(manager: MemoryManager) -> Self {
        Self { manager }
    }
}

impl Tool for ReadMemoryTool {
    fn name(&self) -> &str {
        "read_memory"
    }

    fn description(&self) -> &str {
        "Read persistent memories. Omit name to list all, or provide name to read specific memory."
    }

    fn input_schema(&self) -> JsonValue {
        json::object([
            ("type", json::string("object")),
            (
                "properties",
                json::object([(
                    "name",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string("Memory name to read. Omit to list all."),
                        ),
                    ]),
                )]),
            ),
        ])
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        if let Some(name) = string_field(input, "name").filter(|name| !name.is_empty()) {
            return match self.manager.load(name) {
                Ok(Some(memory)) => ToolResult::success(format!(
                    "[{}] {}\n\n{}",
                    memory.memory_type, memory.description, memory.content
                )),
                Ok(None) => ToolResult::error(format!("Memory '{name}' not found.")),
                Err(error) => ToolResult::error(error),
            };
        }

        let index = self.manager.get_index_content();
        ToolResult::success(if index.is_empty() {
            "No memories saved yet.".into()
        } else {
            index
        })
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }
}

#[derive(Clone, Debug)]
pub struct WriteMemoryTool {
    manager: MemoryManager,
}

impl WriteMemoryTool {
    pub fn new(manager: MemoryManager) -> Self {
        Self { manager }
    }
}

impl Tool for WriteMemoryTool {
    fn name(&self) -> &str {
        "write_memory"
    }

    fn description(&self) -> &str {
        "Save a persistent memory. Use when the user explicitly asks you to remember or preserve information. Choose a concise, stable name, an appropriate type, a short description, and the useful content to keep. Types: feedback, project, reference, user."
    }

    fn input_schema(&self) -> JsonValue {
        json::object([
            ("type", json::string("object")),
            (
                "properties",
                json::object([
                    ("name", json::object([("type", json::string("string"))])),
                    ("content", json::object([("type", json::string("string"))])),
                    (
                        "memory_type",
                        json::object([
                            ("type", json::string("string")),
                            (
                                "enum",
                                json::array(
                                    MEMORY_TYPES
                                        .iter()
                                        .map(|memory_type| json::string((*memory_type).to_owned())),
                                ),
                            ),
                        ]),
                    ),
                    (
                        "description",
                        json::object([("type", json::string("string"))]),
                    ),
                ]),
            ),
            (
                "required",
                json::array([
                    json::string("name"),
                    json::string("content"),
                    json::string("memory_type"),
                    json::string("description"),
                ]),
            ),
        ])
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        for field in ["name", "content", "memory_type", "description"] {
            if string_field(input, field).is_none() {
                return Err(format!("missing required field '{field}'"));
            }
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let Some(name) = string_field(input, "name") else {
            return ToolResult::error("missing required field 'name'");
        };
        let Some(content) = string_field(input, "content") else {
            return ToolResult::error("missing required field 'content'");
        };
        let Some(memory_type) = string_field(input, "memory_type") else {
            return ToolResult::error("missing required field 'memory_type'");
        };
        let Some(description) = string_field(input, "description") else {
            return ToolResult::error("missing required field 'description'");
        };

        match self.manager.save(name, content, memory_type, description) {
            Ok(()) => ToolResult::success(format!("Memory '{name}' saved.")),
            Err(error) => ToolResult::error(error),
        }
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }
}

pub fn register_memory_tools(
    registry: &mut ToolRegistry,
    memory_dir: impl Into<PathBuf>,
) -> io::Result<()> {
    let manager = MemoryManager::new(memory_dir)?;
    registry.register(Box::new(ReadMemoryTool::new(manager.clone())));
    registry.register(Box::new(WriteMemoryTool::new(manager)));
    Ok(())
}

fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    match input {
        JsonValue::Object(fields) => match fields.get(field) {
            Some(JsonValue::String(value)) => Some(value.as_str()),
            _ => None,
        },
        _ => None,
    }
}
