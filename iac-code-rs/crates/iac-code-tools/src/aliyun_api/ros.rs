use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use iac_code_protocol::json::JsonValue;

use crate::{ToolContext, ToolResult};

use super::input::json_value_to_param;

const ROS_PARAMETER_ACTIONS: &[&str] = &[
    "CreateStack",
    "UpdateStack",
    "PreviewStack",
    "CreateChangeSet",
    "GetTemplateEstimateCost",
    "GetTemplateSummary",
    "GetTemplateParameterConstraints",
    "CreateStackGroup",
    "UpdateStackGroup",
];

const ROS_TEMPLATE_ACTIONS: &[&str] = &[
    "ValidateTemplate",
    "CreateStack",
    "UpdateStack",
    "PreviewStack",
    "CreateChangeSet",
    "GetTemplateEstimateCost",
    "GetTemplateSummary",
    "GenerateTemplatePolicy",
    "GetTemplateParameterConstraints",
    "CreateStackGroup",
    "UpdateStackGroup",
    "CreateTemplate",
    "UpdateTemplate",
];

const TERRAFORM_TRANSFORM_PREFIXES: &[&str] = &["Aliyun::Terraform-", "Aliyun::OpenTofu-"];

pub(super) fn expand_ros_parameters(action: &str, params: &mut BTreeMap<String, JsonValue>) {
    if !ROS_PARAMETER_ACTIONS.contains(&action) || !params.contains_key("Parameters") {
        return;
    }
    if params
        .keys()
        .any(|key| key.starts_with("Parameters.") && key.ends_with(".ParameterKey"))
    {
        return;
    }
    let Some(pairs) = params.get("Parameters").and_then(normalize_ros_parameters) else {
        return;
    };

    params.remove("Parameters");
    for (index, (key, value)) in pairs.into_iter().enumerate() {
        let parameter_index = index + 1;
        params.insert(
            format!("Parameters.{parameter_index}.ParameterKey"),
            JsonValue::String(key),
        );
        params.insert(
            format!("Parameters.{parameter_index}.ParameterValue"),
            JsonValue::String(value),
        );
    }
}

fn normalize_ros_parameters(value: &JsonValue) -> Option<Vec<(String, String)>> {
    match value {
        JsonValue::Object(fields) => Some(
            fields
                .iter()
                .filter(|(_, value)| !matches!(value, JsonValue::Null))
                .map(|(key, value)| (key.clone(), json_value_to_param(value)))
                .collect(),
        ),
        JsonValue::Array(items) => {
            let mut result = Vec::new();
            for item in items {
                let JsonValue::Object(fields) = item else {
                    return None;
                };
                let key = match fields.get("ParameterKey") {
                    Some(JsonValue::Null) | None => return None,
                    Some(value) => json_value_to_param(value),
                };
                let Some(value) = fields.get("ParameterValue") else {
                    continue;
                };
                if matches!(value, JsonValue::Null) {
                    continue;
                }
                result.push((key, json_value_to_param(value)));
            }
            Some(result)
        }
        _ => None,
    }
}

pub(super) fn validate_ros_template(
    action: &str,
    params: &BTreeMap<String, String>,
) -> Option<ToolResult> {
    if !ROS_TEMPLATE_ACTIONS.contains(&action) {
        return None;
    }
    let template_body = params.get("TemplateBody").filter(|body| !body.is_empty())?;
    let template = match parse_ros_template(template_body) {
        Ok(template) => template,
        Err(error) => return Some(ToolResult::error(error)),
    };
    let errors = validate_ros_template_structure(&template);
    if errors.is_empty() {
        return None;
    }
    Some(ToolResult::error(format!(
        "Template structure validation found the following issues, please fix and retry:\n{}",
        errors
            .iter()
            .map(|error| format!("  - {error}"))
            .collect::<Vec<_>>()
            .join("\n")
    )))
}

fn parse_ros_template(template_body: &str) -> Result<serde_json::Value, String> {
    if template_body.trim_start().starts_with('{') {
        parse_json_ros_template(template_body)
    } else {
        parse_yaml_ros_template(template_body)
    }
}

fn parse_json_ros_template(template_body: &str) -> Result<serde_json::Value, String> {
    let value = serde_json::from_str::<serde_json::Value>(template_body).map_err(|error| {
        format!(
            "Template JSON syntax error (line {}, column {}): {}",
            error.line(),
            error.column(),
            error
        )
    })?;
    if !value.is_object() {
        return Err(
            "Template JSON parse result is not an object (dict), please check the template format"
                .into(),
        );
    }
    Ok(value)
}

fn parse_yaml_ros_template(template_body: &str) -> Result<serde_json::Value, String> {
    let value = serde_yaml::from_str::<serde_yaml::Value>(template_body)
        .map_err(|error| format_yaml_error(&error))?;
    let value = yaml_value_to_json(&value);
    if !value.is_object() {
        return Err(
            "Template YAML parse result is not an object (dict), please check the template format"
                .into(),
        );
    }
    Ok(value)
}

fn format_yaml_error(error: &serde_yaml::Error) -> String {
    if let Some(location) = error.location() {
        format!(
            "Template YAML syntax error (line {}, column {}): {}",
            location.line(),
            location.column(),
            error
        )
    } else {
        format!("Template YAML syntax error: {error}")
    }
}

fn yaml_value_to_json(value: &serde_yaml::Value) -> serde_json::Value {
    match value {
        serde_yaml::Value::Null => serde_json::Value::Null,
        serde_yaml::Value::Bool(value) => serde_json::Value::Bool(*value),
        serde_yaml::Value::Number(value) => yaml_number_to_json(value),
        serde_yaml::Value::String(value) => serde_json::Value::String(value.clone()),
        serde_yaml::Value::Sequence(values) => {
            serde_json::Value::Array(values.iter().map(yaml_value_to_json).collect())
        }
        serde_yaml::Value::Mapping(values) => {
            let mut object = serde_json::Map::new();
            for (key, value) in values {
                if let Some(key) = yaml_key_to_string(key) {
                    object.insert(key, yaml_value_to_json(value));
                }
            }
            serde_json::Value::Object(object)
        }
        serde_yaml::Value::Tagged(value) => yaml_value_to_json(&value.value),
    }
}

fn yaml_number_to_json(value: &serde_yaml::Number) -> serde_json::Value {
    if let Some(value) = value.as_i64() {
        serde_json::Value::Number(value.into())
    } else if let Some(value) = value.as_u64() {
        serde_json::Value::Number(value.into())
    } else if let Some(value) = value.as_f64() {
        serde_json::Number::from_f64(value)
            .map_or(serde_json::Value::Null, serde_json::Value::Number)
    } else {
        serde_json::Value::Null
    }
}

fn yaml_key_to_string(value: &serde_yaml::Value) -> Option<String> {
    match value {
        serde_yaml::Value::String(value) => Some(value.clone()),
        serde_yaml::Value::Bool(value) => Some(value.to_string()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        serde_yaml::Value::Tagged(value) => yaml_key_to_string(&value.value),
        serde_yaml::Value::Null
        | serde_yaml::Value::Sequence(_)
        | serde_yaml::Value::Mapping(_) => None,
    }
}

fn validate_ros_template_structure(template: &serde_json::Value) -> Vec<String> {
    let mut errors = Vec::new();
    if template.get("ROSTemplateFormatVersion").is_none() {
        errors.push(
            "Template is missing ROSTemplateFormatVersion (ROS templates must include this field, e.g. '2015-09-01')"
                .into(),
        );
    }

    if is_terraform_template(template) {
        return errors;
    }

    match template.get("Resources") {
        None => errors
            .push("Template is missing Resources (ROS templates must include Resources)".into()),
        Some(resources) if !resources.is_object() => errors.push(format!(
            "Resources must be an object (dict), current type is {}",
            json_type_name(resources)
        )),
        Some(resources) => {
            let Some(resources) = resources.as_object() else {
                return errors;
            };
            for (name, resource) in resources {
                if !resource.is_object() {
                    errors.push(format!(
                        "Resource '{name}' definition must be an object (dict), current type is {}",
                        json_type_name(resource)
                    ));
                    continue;
                }
                let Some(resource_type) = resource.get("Type") else {
                    errors.push(format!("Resource '{name}' is missing the Type field"));
                    continue;
                };
                let Some(resource_type) = resource_type.as_str() else {
                    continue;
                };
                if let Some(correct_type) = corrected_ros_resource_type(resource_type) {
                    errors.push(format!(
                        "Resource '{name}' has incorrect type '{resource_type}', should be '{correct_type}'"
                    ));
                }
            }
        }
    }
    errors
}

fn is_terraform_template(template: &serde_json::Value) -> bool {
    let Some(transform) = template.get("Transform") else {
        return false;
    };
    match transform {
        serde_json::Value::String(value) => TERRAFORM_TRANSFORM_PREFIXES
            .iter()
            .any(|prefix| value.starts_with(prefix)),
        serde_json::Value::Array(values) => values.iter().any(|value| {
            value.as_str().is_some_and(|text| {
                TERRAFORM_TRANSFORM_PREFIXES
                    .iter()
                    .any(|prefix| text.starts_with(prefix))
            })
        }),
        _ => false,
    }
}

fn corrected_ros_resource_type(resource_type: &str) -> Option<&'static str> {
    match resource_type {
        "ALIYUN::VPC::VPC" => Some("ALIYUN::ECS::VPC"),
        "ALIYUN::VPC::VSwitch" => Some("ALIYUN::ECS::VSwitch"),
        _ => None,
    }
}

fn json_type_name(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "NoneType",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "int",
        serde_json::Value::String(_) => "str",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "dict",
    }
}

pub(super) fn inline_ros_template_url(
    params: &mut BTreeMap<String, String>,
    context: &ToolContext,
) {
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
