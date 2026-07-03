use iac_code_protocol::json::JsonValue;

pub(super) fn missing_host(url: &str) -> bool {
    let Some((_, rest)) = url.split_once("://") else {
        return true;
    };
    rest.split(['/', '?', '#']).next().is_none_or(str::is_empty)
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

pub(super) fn number_field(input: &JsonValue, field: &str) -> Option<i64> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::Number(value)) => value.parse::<i64>().ok(),
        _ => None,
    }
}
