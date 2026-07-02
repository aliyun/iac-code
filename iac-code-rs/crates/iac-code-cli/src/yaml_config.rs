pub(super) fn yaml_mapping_get<'a>(
    mapping: &'a serde_yaml::Mapping,
    key: &str,
) -> Option<&'a serde_yaml::Value> {
    mapping.get(serde_yaml::Value::String(key.to_owned()))
}

pub(super) fn yaml_string_list(mapping: &serde_yaml::Mapping, key: &str) -> Vec<String> {
    let Some(serde_yaml::Value::Sequence(items)) = yaml_mapping_get(mapping, key) else {
        return Vec::new();
    };
    items.iter().filter_map(yaml_coerced_string).collect()
}

fn yaml_coerced_string(value: &serde_yaml::Value) -> Option<String> {
    match value {
        serde_yaml::Value::Null => None,
        serde_yaml::Value::Bool(value) => Some(if *value { "True" } else { "False" }.to_owned()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        serde_yaml::Value::String(value) => Some(value.clone()),
        serde_yaml::Value::Sequence(_)
        | serde_yaml::Value::Mapping(_)
        | serde_yaml::Value::Tagged(_) => serde_yaml::to_string(value)
            .ok()
            .map(|value| value.trim_end().to_owned())
            .filter(|value| !value.is_empty()),
    }
}
