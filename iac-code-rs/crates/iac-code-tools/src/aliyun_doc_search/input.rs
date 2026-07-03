use iac_code_protocol::json::JsonValue;

pub(super) fn build_search_params(
    input: &JsonValue,
    keywords: &str,
    page_size: usize,
) -> Vec<(&'static str, String)> {
    let mut params = vec![
        ("keywords", keywords.to_owned()),
        ("topics", "DOCUMENT,PRODUCT".to_owned()),
        ("language", "zh".to_owned()),
        ("website", "cn".to_owned()),
        ("pageSize", page_size.to_string()),
        ("pageNum", "1".to_owned()),
    ];
    if let Some(category_id) = scalar_field(input, "category_id") {
        params.push(("categoryId", category_id));
    }
    params
}

pub(super) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn scalar_field(input: &JsonValue, field: &str) -> Option<String> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    fields.get(field).and_then(scalar_value)
}

fn scalar_value(value: &JsonValue) -> Option<String> {
    match value {
        JsonValue::String(value) | JsonValue::Number(value) => Some(value.clone()),
        _ => None,
    }
}
