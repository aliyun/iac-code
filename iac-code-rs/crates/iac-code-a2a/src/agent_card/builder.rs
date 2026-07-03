use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};

use crate::exposure::format_a2a_exposure_slice;
use crate::signing;

use super::{
    AgentCardOptions, AgentInterfaceConfig, IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI,
    IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI, VERSION,
};

pub fn build_agent_card(options: AgentCardOptions) -> JsonValue {
    let input_modes = supported_input_mime_types();
    let interfaces = if options.supported_interfaces.is_empty() {
        vec![AgentInterfaceConfig::new(
            format!("http://{}:{}/", options.host, options.port),
            "JSONRPC",
            "1.0",
        )]
    } else {
        options.supported_interfaces.clone()
    };

    let mut description =
        "AI-powered Infrastructure as Code assistant for Alibaba Cloud ROS and Terraform workflows."
            .to_owned();
    if !options.token_enabled && !options.basic_enabled && !options.api_key_enabled {
        description.push_str(
            " Unauthenticated A2A server mode is intended for trusted local environments.",
        );
    }
    if options.push_notifications {
        description.push_str(
            " Experimental terminal-state webhooks can be enabled locally, but the standard A2A push config API is not advertised.",
        );
    }

    let mut card = BTreeMap::new();
    card.insert("name".to_owned(), json::string("iac-code"));
    card.insert("description".to_owned(), json::string(description));
    card.insert(
        "supportedInterfaces".to_owned(),
        json::array(interfaces.iter().map(AgentInterfaceConfig::to_json)),
    );
    card.insert(
        "provider".to_owned(),
        json::object([("organization", json::string("iac-code"))]),
    );
    card.insert("version".to_owned(), json::string(VERSION));
    card.insert("capabilities".to_owned(), capabilities_json(&options));
    card.insert("defaultInputModes".to_owned(), string_array(&input_modes));
    card.insert(
        "defaultOutputModes".to_owned(),
        string_array(&["text/plain"]),
    );
    card.insert("skills".to_owned(), skills_json(&input_modes));

    if options.token_enabled || options.basic_enabled || options.api_key_enabled {
        add_security(&mut card, &options);
    }

    let mut value = JsonValue::Object(card);
    if let Some(secret) = &options.signing_secret {
        add_signature(&mut value, secret, &options.signing_key_id);
    }
    value
}

pub fn agent_card_to_client_dict(card: &JsonValue) -> JsonValue {
    let JsonValue::Object(card_object) = card else {
        return card.clone();
    };
    let mut data = card_object.clone();
    let Some(JsonValue::Array(interfaces)) = data.get("supportedInterfaces").cloned() else {
        return JsonValue::Object(data);
    };
    let Some(JsonValue::Object(primary)) = interfaces.first() else {
        return JsonValue::Object(data);
    };
    if let Some(url) = primary.get("url") {
        data.entry("url".to_owned()).or_insert_with(|| url.clone());
    }
    if let Some(protocol_binding) = primary.get("protocolBinding") {
        data.entry("preferredTransport".to_owned())
            .or_insert_with(|| protocol_binding.clone());
    }
    if let Some(protocol_version) = primary.get("protocolVersion") {
        data.entry("protocolVersion".to_owned())
            .or_insert_with(|| protocol_version.clone());
    }

    let additional = interfaces
        .iter()
        .skip(1)
        .filter_map(|interface| {
            let JsonValue::Object(interface) = interface else {
                return None;
            };
            let url = interface.get("url")?.clone();
            let transport = interface.get("protocolBinding")?.clone();
            Some(json::object([("transport", transport), ("url", url)]))
        })
        .collect::<Vec<_>>();
    if !additional.is_empty() {
        data.entry("additionalInterfaces".to_owned())
            .or_insert_with(|| JsonValue::Array(additional));
    }
    JsonValue::Object(data)
}

fn capabilities_json(options: &AgentCardOptions) -> JsonValue {
    let mut extensions = vec![json::object([
        (
            "description",
            json::string(
                "Optional iac-code metadata namespace for tool status and stored local artifact metadata.",
            ),
        ),
        ("required", json::bool_value(false)),
        ("uri", json::string(IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI)),
    ])];
    if !options.thinking_exposure_types.is_empty() {
        extensions.push(json::object([
            (
                "description",
                json::string(
                    "Optional iac-code metadata namespace for selected thinking exposure signals. Raw thinking is emitted only when raw_thinking is enabled.",
                ),
            ),
            (
                "params",
                json::object([(
                    "enabledTypes",
                    string_array(&format_a2a_exposure_slice(&options.thinking_exposure_types)),
                )]),
            ),
            ("required", json::bool_value(false)),
            ("uri", json::string(IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI)),
        ]));
    }
    extensions.extend(
        options
            .agent_extensions
            .iter()
            .map(|extension| extension.to_json()),
    );

    json::object([
        ("extendedAgentCard", json::bool_value(true)),
        ("extensions", JsonValue::Array(extensions)),
        (
            "pushNotifications",
            json::bool_value(options.push_notifications),
        ),
        ("streaming", json::bool_value(true)),
    ])
}

fn add_security(card: &mut BTreeMap<String, JsonValue>, options: &AgentCardOptions) {
    let mut schemes = BTreeMap::new();
    let mut requirements = Vec::new();

    if options.token_enabled {
        schemes.insert(
            "bearerAuth".to_owned(),
            json::object([(
                "httpAuthSecurityScheme",
                json::object([("scheme", json::string("bearer"))]),
            )]),
        );
        requirements.push(security_requirement("bearerAuth"));
    }
    if options.basic_enabled {
        schemes.insert(
            "basicAuth".to_owned(),
            json::object([(
                "httpAuthSecurityScheme",
                json::object([("scheme", json::string("basic"))]),
            )]),
        );
        requirements.push(security_requirement("basicAuth"));
    }
    if options.api_key_enabled {
        schemes.insert(
            "apiKeyAuth".to_owned(),
            json::object([(
                "apiKeySecurityScheme",
                json::object([
                    ("location", json::string("header")),
                    ("name", json::string(&options.api_key_header)),
                ]),
            )]),
        );
        requirements.push(security_requirement("apiKeyAuth"));
    }

    card.insert("securitySchemes".to_owned(), JsonValue::Object(schemes));
    card.insert(
        "securityRequirements".to_owned(),
        JsonValue::Array(requirements),
    );
}

fn security_requirement(scheme_name: &str) -> JsonValue {
    json::object([(
        "schemes",
        json::object([(scheme_name, json::object([("list", string_array(&[""]))]))]),
    )])
}

fn skills_json(input_modes: &[&str]) -> JsonValue {
    json::array([
        skill_json(
            "iac_generation",
            "IaC Generation",
            "Generate Alibaba Cloud ROS and Terraform templates from natural language.",
            &["iac", "ros", "terraform", "alibaba-cloud"],
            "Create a VPC with two vSwitches in cn-hangzhou.",
            input_modes,
        ),
        skill_json(
            "iac_review",
            "IaC Review",
            "Inspect IaC templates and suggest fixes.",
            &["iac", "review", "validation"],
            "Review this ROS template for missing parameters.",
            input_modes,
        ),
        skill_json(
            "aliyun_ros_operations",
            "Alibaba Cloud ROS Operations",
            "Assist with ROS stack workflows using iac-code tools.",
            &["aliyun", "ros", "stack"],
            "Check why this ROS stack update failed.",
            input_modes,
        ),
        skill_json(
            "terraform_ros_conversion",
            "Terraform To ROS Conversion",
            "Assist Terraform-to-ROS conversion using bundled iac-code skill resources.",
            &["terraform", "ros", "conversion"],
            "Convert this Terraform VPC module to ROS YAML.",
            input_modes,
        ),
    ])
}

fn skill_json(
    id: &str,
    name: &str,
    description: &str,
    tags: &[&str],
    example: &str,
    input_modes: &[&str],
) -> JsonValue {
    json::object([
        ("description", json::string(description)),
        ("examples", string_array(&[example])),
        ("id", json::string(id)),
        ("inputModes", string_array(input_modes)),
        ("name", json::string(name)),
        ("outputModes", string_array(&["text/plain"])),
        ("tags", string_array(tags)),
    ])
}

fn supported_input_mime_types() -> Vec<&'static str> {
    vec![
        "text/plain",
        "application/json",
        "text/markdown",
        "text/yaml",
        "application/yaml",
        "application/x-yaml",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "application/octet-stream",
    ]
}

fn string_array(values: &[&str]) -> JsonValue {
    json::array(values.iter().map(|value| json::string(*value)))
}

fn add_signature(card: &mut JsonValue, secret: &str, key_id: &str) {
    *card = signing::sign_agent_card_dict(card, secret, key_id);
}
