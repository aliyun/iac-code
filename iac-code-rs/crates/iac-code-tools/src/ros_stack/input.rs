use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use iac_code_protocol::json::JsonValue;

use crate::ToolContext;

pub(crate) fn params_with_region_and_template(
    input: &JsonValue,
    region: &str,
    context: &ToolContext,
) -> BTreeMap<String, String> {
    let mut params = object_string_map(object_field(input, "params"));
    if !region.is_empty() {
        params
            .entry("RegionId".into())
            .or_insert_with(|| region.to_owned());
    }
    inline_template_url(&mut params, context);
    params
}

fn inline_template_url(params: &mut BTreeMap<String, String>, context: &ToolContext) {
    let Some(template_url) = params.get("TemplateURL").cloned() else {
        return;
    };
    if template_url.starts_with("http://")
        || template_url.starts_with("https://")
        || template_url.starts_with("oss://")
    {
        return;
    }
    let path = PathBuf::from(&template_url);
    let path = if path.is_absolute() {
        path
    } else {
        PathBuf::from(&context.cwd).join(path)
    };
    if let Ok(content) = fs::read_to_string(path) {
        params.remove("TemplateURL");
        params.insert("TemplateBody".into(), content);
    }
}

fn object_string_map(input: Option<&BTreeMap<String, JsonValue>>) -> BTreeMap<String, String> {
    input
        .map(|fields| {
            fields
                .iter()
                .map(|(key, value)| (key.clone(), json_value_to_param(value)))
                .collect()
        })
        .unwrap_or_default()
}

fn json_value_to_param(value: &JsonValue) -> String {
    match value {
        JsonValue::String(value) | JsonValue::Number(value) => value.clone(),
        JsonValue::Bool(value) => {
            if *value {
                "true".into()
            } else {
                "false".into()
            }
        }
        JsonValue::Null => "null".into(),
        JsonValue::Array(_) | JsonValue::Object(_) => value.to_compact_json(),
    }
}

pub(crate) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn object_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a BTreeMap<String, JsonValue>> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    }
}
