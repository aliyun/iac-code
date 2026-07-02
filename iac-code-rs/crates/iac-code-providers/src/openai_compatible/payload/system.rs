use iac_code_protocol::json::{self, JsonValue};

const DYNAMIC_BOUNDARY: &str = "--- DYNAMIC_BOUNDARY ---";

pub(super) fn build_system_message(provider_key: &str, model: &str, system: &str) -> JsonValue {
    if is_dashscope_explicit_cache_model(provider_key, model) {
        let (static_part, dynamic_part) = split_by_dynamic_boundary(system);
        let mut content = vec![cacheable_text_part(static_part)];
        if !dynamic_part.is_empty() {
            content.push(text_part(dynamic_part));
        }
        return json::object([
            ("role", json::string("system")),
            ("content", json::array(content)),
        ]);
    }

    json::object([
        ("role", json::string("system")),
        ("content", json::string(system)),
    ])
}

pub(super) fn is_dashscope_explicit_cache_model(provider_key: &str, model: &str) -> bool {
    let is_dashscope_provider = matches!(
        provider_key,
        "dashscope" | "dashscope_token_plan" | "aliyun_codingplan" | "aliyun_codingplan_intl"
    );
    is_dashscope_provider
        && [
            "qwen3-coder-plus",
            "qwen3-coder-flash",
            "qwen3.5-plus",
            "qwen3.6-plus",
            "qwen-plus",
            "qwen3.5-flash",
            "qwen3.6-flash",
            "qwen-flash",
        ]
        .iter()
        .any(|prefix| model.starts_with(prefix))
}

pub(super) fn mark_last_user_message_cacheable(messages: &mut [JsonValue]) {
    for message in messages.iter_mut().rev() {
        let JsonValue::Object(fields) = message else {
            continue;
        };
        let is_user = matches!(fields.get("role"), Some(JsonValue::String(role)) if role == "user");
        if !is_user {
            continue;
        }

        let Some(content) = fields.get_mut("content") else {
            break;
        };
        match content {
            JsonValue::String(text) => {
                let text = std::mem::take(text);
                *content = json::array([cacheable_text_part(&text)]);
            }
            JsonValue::Array(blocks) => {
                for block in blocks.iter_mut().rev() {
                    let JsonValue::Object(block_fields) = block else {
                        continue;
                    };
                    let is_text = matches!(
                        block_fields.get("type"),
                        Some(JsonValue::String(block_type)) if block_type == "text"
                    );
                    if is_text {
                        block_fields.insert("cache_control".into(), cache_control_value());
                        break;
                    }
                }
            }
            _ => {}
        }
        break;
    }
}

fn split_by_dynamic_boundary(system: &str) -> (&str, &str) {
    let Some(index) = system.find(DYNAMIC_BOUNDARY) else {
        return (system, "");
    };
    let static_part = &system[..index];
    let dynamic_part = &system[index + DYNAMIC_BOUNDARY.len()..];
    (static_part.trim_end(), dynamic_part.trim_start())
}

fn cacheable_text_part(text: &str) -> JsonValue {
    json::object([
        ("type", json::string("text")),
        ("text", json::string(text)),
        ("cache_control", cache_control_value()),
    ])
}

fn text_part(text: &str) -> JsonValue {
    json::object([("type", json::string("text")), ("text", json::string(text))])
}

fn cache_control_value() -> JsonValue {
    json::object([("type", json::string("ephemeral"))])
}
