use std::collections::BTreeMap;

pub(super) fn parse_a2a_client_config_content(
    content: &str,
) -> Result<BTreeMap<String, String>, String> {
    let value =
        serde_yaml::from_str::<serde_yaml::Value>(content).map_err(|error| error.to_string())?;
    a2a_client_config_from_yaml_value(&value)
}

fn a2a_client_config_from_yaml_value(
    value: &serde_yaml::Value,
) -> Result<BTreeMap<String, String>, String> {
    let mapping = value
        .as_mapping()
        .ok_or_else(|| "A2A config file must contain a YAML mapping.".to_owned())?;
    let mut config = BTreeMap::new();
    for (key, value) in mapping {
        let Some(key) = key.as_str() else {
            continue;
        };
        let key = normalize_a2a_config_key(key);
        if matches!(key.as_str(), "route" | "routes") {
            let routes = yaml_a2a_route_specs(value)?;
            if !routes.is_empty() {
                config.insert(key, routes.join("\n"));
            }
            continue;
        }
        if let Some(value) = yaml_config_value(value) {
            config.insert(key, value);
        }
    }
    Ok(config)
}

fn yaml_a2a_route_specs(value: &serde_yaml::Value) -> Result<Vec<String>, String> {
    match value {
        serde_yaml::Value::String(route) => Ok(vec![route.clone()]),
        serde_yaml::Value::Sequence(items) => items.iter().map(yaml_a2a_route_spec).collect(),
        _ => Ok(Vec::new()),
    }
}

fn yaml_a2a_route_spec(value: &serde_yaml::Value) -> Result<String, String> {
    match value {
        serde_yaml::Value::String(route) => Ok(route.clone()),
        serde_yaml::Value::Mapping(route) => {
            let name = yaml_mapping_field(route, "name")
                .and_then(serde_yaml::Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    "A2A client route config entries require name and url.".to_owned()
                })?;
            let url = yaml_mapping_field(route, "url")
                .and_then(serde_yaml::Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    "A2A client route config entries require name and url.".to_owned()
                })?;
            let mut parts = vec![format!("{name}={url}")];
            if let Some(skills) = yaml_mapping_field(route, "skills")
                .map(|value| yaml_string_or_string_list(value, "skills"))
                .transpose()?
                .flatten()
            {
                if !skills.is_empty() {
                    parts.push(format!("skills={skills}"));
                }
            }
            if let Some(tags) = yaml_mapping_field(route, "tags")
                .map(|value| yaml_string_or_string_list(value, "tags"))
                .transpose()?
                .flatten()
            {
                if !tags.is_empty() {
                    parts.push(format!("tags={tags}"));
                }
            }
            Ok(parts.join(";"))
        }
        _ => Err("A2A client routes config entries must be strings or mappings.".to_owned()),
    }
}

fn yaml_config_value(value: &serde_yaml::Value) -> Option<String> {
    match value {
        serde_yaml::Value::Null => None,
        serde_yaml::Value::Bool(value) => Some(value.to_string()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        serde_yaml::Value::String(value) => Some(value.clone()),
        serde_yaml::Value::Sequence(items) => {
            let values = items
                .iter()
                .filter_map(yaml_scalar_string)
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>();
            (!values.is_empty()).then(|| values.join("\n"))
        }
        _ => None,
    }
}

fn yaml_string_or_string_list(
    value: &serde_yaml::Value,
    key: &str,
) -> Result<Option<String>, String> {
    match value {
        serde_yaml::Value::String(value) => Ok(Some(value.clone())),
        serde_yaml::Value::Sequence(items) => {
            let values = items
                .iter()
                .filter_map(yaml_scalar_string)
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>();
            Ok(Some(values.join(",")))
        }
        _ => Err(format!("A2A client route {key} must be a string or list.")),
    }
}

fn yaml_scalar_string(value: &serde_yaml::Value) -> Option<String> {
    match value {
        serde_yaml::Value::Bool(value) => Some(value.to_string()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        serde_yaml::Value::String(value) => Some(value.clone()),
        _ => None,
    }
}

fn yaml_mapping_field<'a>(
    mapping: &'a serde_yaml::Mapping,
    key: &str,
) -> Option<&'a serde_yaml::Value> {
    mapping
        .iter()
        .find_map(|(field, value)| (field.as_str() == Some(key)).then_some(value))
}

fn normalize_a2a_config_key(key: &str) -> String {
    key.replace('-', "_")
}
