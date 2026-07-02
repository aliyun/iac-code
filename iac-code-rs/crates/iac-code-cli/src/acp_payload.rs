use std::collections::BTreeMap;

use iac_code_acp::convert::{AcpContentBlock, AvailableCommand, SessionUpdate};
use iac_code_acp::permissions::{PermissionOption, PermissionToolCall};
use iac_code_acp::session::PromptResponse;
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{load_saved_model, DEFAULT_MODEL};
use iac_code_protocol::{json, json::JsonValue};

use crate::cli_i18n::tr;
use crate::json_utils::{json_object_field, json_string_field};
use crate::jsonrpc_payload::empty_json_object;

pub(super) fn acp_initialize_response(version: &str) -> JsonValue {
    json::object([
        ("protocolVersion", json::number(1)),
        (
            "agentCapabilities",
            json::object([
                ("loadSession", json::bool_value(true)),
                (
                    "promptCapabilities",
                    json::object([
                        ("embeddedContext", json::bool_value(true)),
                        ("image", json::bool_value(false)),
                        ("audio", json::bool_value(false)),
                    ]),
                ),
                (
                    "mcpCapabilities",
                    json::object([
                        ("http", json::bool_value(false)),
                        ("sse", json::bool_value(false)),
                    ]),
                ),
                (
                    "sessionCapabilities",
                    json::object([
                        ("close", empty_json_object()),
                        ("list", empty_json_object()),
                    ]),
                ),
            ]),
        ),
        (
            "authMethods",
            json::array([
                acp_env_auth_method(
                    "DASHSCOPE_API_KEY",
                    "DashScope / Qwen API Key",
                    "https://dashscope.console.aliyun.com/",
                ),
                acp_env_auth_method(
                    "OPENAI_API_KEY",
                    "OpenAI API Key",
                    "https://platform.openai.com/api-keys",
                ),
                acp_env_auth_method(
                    "ANTHROPIC_API_KEY",
                    "Anthropic API Key",
                    "https://console.anthropic.com/",
                ),
                acp_env_auth_method(
                    "DEEPSEEK_API_KEY",
                    "DeepSeek API Key",
                    "https://platform.deepseek.com/",
                ),
            ]),
        ),
        (
            "agentInfo",
            json::object([
                ("name", json::string("iac-code")),
                ("version", json::string(version)),
            ]),
        ),
    ])
}

fn acp_env_auth_method(env_name: &str, label: &str, link: &str) -> JsonValue {
    json::object([
        ("type", json::string("env_var")),
        (
            "id",
            json::string(format!("env_{}", env_name.to_ascii_lowercase())),
        ),
        ("name", json::string(label)),
        (
            "description",
            json::string(format!(
                "Set {env_name} to authenticate with this provider."
            )),
        ),
        ("link", json::string(link)),
        (
            "vars",
            json::array([json::object([
                ("name", json::string(env_name)),
                ("label", json::string(label)),
                ("secret", json::bool_value(true)),
                ("optional", json::bool_value(false)),
            ])]),
        ),
    ])
}

pub(super) fn acp_model_state_json() -> JsonValue {
    let model = ConfigPaths::from_env()
        .ok()
        .and_then(|paths| load_saved_model(&paths).ok().flatten())
        .unwrap_or_else(|| DEFAULT_MODEL.to_owned());
    json::object([
        (
            "availableModels",
            json::array([json::object([
                ("modelId", json::string(&model)),
                ("name", json::string(&model)),
                (
                    "description",
                    json::string("Active model configured for iac-code"),
                ),
            ])]),
        ),
        ("currentModelId", json::string(model)),
    ])
}

pub(super) fn acp_prompt_blocks(params: &JsonValue) -> Vec<AcpContentBlock> {
    let Some(JsonValue::Array(blocks)) = json_object_field(params, "prompt") else {
        return Vec::new();
    };
    blocks.iter().map(acp_content_block).collect()
}

fn acp_content_block(value: &JsonValue) -> AcpContentBlock {
    let type_name = json_string_field(value, "type").unwrap_or("unknown");
    match type_name {
        "text" => AcpContentBlock::Text {
            text: json_string_field(value, "text")
                .unwrap_or_default()
                .to_owned(),
        },
        "resource_link" => AcpContentBlock::ResourceLink {
            uri: json_string_field(value, "uri")
                .unwrap_or_default()
                .to_owned(),
            name: json_string_field(value, "name")
                .unwrap_or_default()
                .to_owned(),
        },
        "resource" => {
            let Some(resource) = json_object_field(value, "resource") else {
                return AcpContentBlock::Unsupported {
                    type_name: type_name.to_owned(),
                };
            };
            AcpContentBlock::EmbeddedTextResource {
                uri: json_string_field(resource, "uri")
                    .unwrap_or_default()
                    .to_owned(),
                text: json_string_field(resource, "text")
                    .unwrap_or_default()
                    .to_owned(),
            }
        }
        "image" => AcpContentBlock::Image {
            mime_type: json_string_field(value, "mimeType")
                .or_else(|| json_string_field(value, "mime_type"))
                .unwrap_or("application/octet-stream")
                .to_owned(),
            data: json_string_field(value, "data")
                .unwrap_or_default()
                .to_owned(),
        },
        "audio" => AcpContentBlock::Audio {
            mime_type: json_string_field(value, "mimeType")
                .or_else(|| json_string_field(value, "mime_type"))
                .unwrap_or("application/octet-stream")
                .to_owned(),
            data: json_string_field(value, "data")
                .unwrap_or_default()
                .to_owned(),
        },
        other => AcpContentBlock::Unsupported {
            type_name: other.to_owned(),
        },
    }
}

pub(super) fn acp_prompt_response_json(response: &PromptResponse) -> JsonValue {
    let mut fields =
        BTreeMap::from([("stopReason".to_owned(), json::string(&response.stop_reason))]);
    if !response.field_meta.is_empty() {
        fields.insert(
            "_meta".to_owned(),
            JsonValue::Object(response.field_meta.clone()),
        );
    }
    JsonValue::Object(fields)
}

pub(super) fn acp_session_update_message(session_id: &str, update: &SessionUpdate) -> JsonValue {
    json::object([
        ("jsonrpc", json::string("2.0")),
        ("method", json::string("session/update")),
        (
            "params",
            json::object([
                ("sessionId", json::string(session_id)),
                ("update", session_update_json(update)),
            ]),
        ),
    ])
}

pub(super) fn acp_permission_request_message(
    request_id: &str,
    session_id: &str,
    options: &[PermissionOption],
    tool_call: &PermissionToolCall,
) -> JsonValue {
    json::object([
        ("id", json::string(request_id)),
        ("jsonrpc", json::string("2.0")),
        ("method", json::string("session/request_permission")),
        (
            "params",
            json::object([
                (
                    "options",
                    json::array(options.iter().map(permission_option_json)),
                ),
                ("sessionId", json::string(session_id)),
                ("toolCall", permission_tool_call_json(tool_call)),
            ]),
        ),
    ])
}

pub(super) fn acp_available_commands_message(session_id: &str) -> JsonValue {
    acp_session_update_message(session_id, &acp_available_commands_update())
}

pub(super) fn acp_available_commands_update() -> SessionUpdate {
    SessionUpdate::available_commands(vec![
        AvailableCommand::new("clear", tr("Clear conversation history"), None),
        AvailableCommand::new("compact", tr("Compact conversation context"), None),
        AvailableCommand::new(
            "debug",
            tr("Toggle debug logging"),
            Some("[on|off]".to_owned()),
        ),
        AvailableCommand::new(
            "memory",
            tr("View and manage persistent memories"),
            Some(tr("[<name>|search <query>|delete <name>|help]")),
        ),
        AvailableCommand::new(
            "rename",
            tr("Rename the current session"),
            Some("<name>".to_owned()),
        ),
    ])
}

pub(super) fn session_update_json(update: &SessionUpdate) -> JsonValue {
    let mut fields = BTreeMap::from([(
        "sessionUpdate".to_owned(),
        json::string(&update.session_update),
    )]);
    match update.session_update.as_str() {
        "user_message_chunk" | "agent_message_chunk" | "agent_thought_chunk" => {
            if let Some(text) = update.content_text() {
                fields.insert("content".to_owned(), text_content_json(text));
            }
        }
        "tool_call" | "tool_call_update" => {
            if let Some(tool_call_id) = &update.tool_call_id {
                fields.insert("toolCallId".to_owned(), json::string(tool_call_id));
            }
            if let Some(title) = &update.title {
                fields.insert("title".to_owned(), json::string(title));
            }
            if let Some(kind) = &update.kind {
                fields.insert("kind".to_owned(), json::string(kind));
            }
            if let Some(status) = update.status {
                fields.insert("status".to_owned(), json::string(status.as_str()));
            }
            if !update.contents.is_empty() {
                fields.insert("content".to_owned(), tool_contents_json(&update.contents));
            }
        }
        "plan" => {
            fields.insert(
                "entries".to_owned(),
                json::array(update.entries.iter().map(|entry| {
                    json::object([
                        ("content", json::string(&entry.content)),
                        ("status", json::string(&entry.status)),
                        ("priority", json::string(&entry.priority)),
                    ])
                })),
            );
        }
        "usage_update" => {
            if let Some(used) = update.used {
                fields.insert("used".to_owned(), json::number(used));
            }
            if let Some(size) = update.size {
                fields.insert("size".to_owned(), json::number(size));
            }
        }
        "available_commands_update" => {
            fields.insert(
                "availableCommands".to_owned(),
                json::array(update.available_commands.iter().map(available_command_json)),
            );
        }
        _ => {}
    }
    if let Some(field_meta) = &update.field_meta {
        fields.insert("_meta".to_owned(), JsonValue::Object(field_meta.clone()));
    }
    JsonValue::Object(fields)
}

fn available_command_json(command: &AvailableCommand) -> JsonValue {
    let mut fields = BTreeMap::from([
        ("name".to_owned(), json::string(&command.name)),
        ("description".to_owned(), json::string(&command.description)),
    ]);
    if let Some(hint) = &command.input_hint {
        fields.insert(
            "input".to_owned(),
            json::object([("root", json::object([("hint", json::string(hint))]))]),
        );
    }
    JsonValue::Object(fields)
}

fn text_content_json(text: &str) -> JsonValue {
    json::object([("type", json::string("text")), ("text", json::string(text))])
}

fn tool_contents_json(contents: &[String]) -> JsonValue {
    json::array(contents.iter().map(|content| {
        json::object([
            ("type", json::string("content")),
            ("content", text_content_json(content)),
        ])
    }))
}

pub(super) fn permission_option_json(option: &PermissionOption) -> JsonValue {
    json::object([
        ("optionId", json::string(&option.option_id)),
        ("name", json::string(&option.name)),
        ("kind", json::string(&option.kind)),
    ])
}

pub(super) fn permission_tool_call_json(tool_call: &PermissionToolCall) -> JsonValue {
    json::object([
        ("toolCallId", json::string(&tool_call.tool_call_id)),
        ("title", json::string(&tool_call.title)),
        ("content", tool_contents_json(&tool_call.content)),
    ])
}
