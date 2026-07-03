use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde_yaml::{Mapping, Value};

use super::{default_formats, MultiModalSpec};

pub(super) fn load_settings_overrides(path: &Path) -> BTreeMap<String, MultiModalSpec> {
    let Ok(content) = fs::read_to_string(path) else {
        return BTreeMap::new();
    };
    let Ok(root) = serde_yaml::from_str::<Value>(&content) else {
        return BTreeMap::new();
    };
    let Some(root_map) = root.as_mapping() else {
        return BTreeMap::new();
    };
    let Some(models) = mapping_get(root_map, "multiModal")
        .and_then(Value::as_mapping)
        .and_then(|section| mapping_get(section, "models"))
        .and_then(Value::as_mapping)
    else {
        return BTreeMap::new();
    };

    models
        .iter()
        .filter_map(|(name, value)| {
            let name = scalar_to_string(name)?;
            let model = value.as_mapping()?;
            Some((name, multimodal_spec_from_yaml(model)))
        })
        .collect()
}

fn multimodal_spec_from_yaml(model: &Mapping) -> MultiModalSpec {
    MultiModalSpec {
        support_multimodal: mapping_get(model, "supportMultimodal")
            .map(python_truthy)
            .unwrap_or(false),
        formats: mapping_get(model, "formats")
            .and_then(formats_from_yaml)
            .unwrap_or_else(default_formats),
        max_images_per_message: mapping_get(model, "maxImagesPerMessage")
            .and_then(usize_from_yaml)
            .unwrap_or(20),
    }
}

fn mapping_get<'a>(mapping: &'a Mapping, key: &str) -> Option<&'a Value> {
    mapping
        .iter()
        .find_map(|(entry_key, value)| (entry_key.as_str() == Some(key)).then_some(value))
}

fn scalar_to_string(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_i64().is_none_or(|number| number != 0),
        Value::String(value) => !value.is_empty(),
        Value::Sequence(value) => !value.is_empty(),
        Value::Mapping(value) => !value.is_empty(),
        Value::Null => false,
        _ => true,
    }
}

fn formats_from_yaml(value: &Value) -> Option<Vec<String>> {
    match value {
        Value::Sequence(values) => Some(values.iter().filter_map(scalar_to_string).collect()),
        Value::String(value) => Some(value.chars().map(|ch| ch.to_string()).collect()),
        _ => None,
    }
}

fn usize_from_yaml(value: &Value) -> Option<usize> {
    match value {
        Value::Number(value) => value.as_u64().and_then(|number| number.try_into().ok()),
        Value::String(value) => value.parse().ok(),
        _ => None,
    }
}
