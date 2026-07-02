use iac_code_protocol::json::JsonValue;

pub(super) fn format_search_response(data: &JsonValue, keywords: &str) -> String {
    let documents = object_field(data, "data")
        .and_then(|data| data.get("documents").and_then(as_object))
        .cloned()
        .unwrap_or_default();
    let items = documents
        .get("data")
        .and_then(as_array)
        .cloned()
        .unwrap_or_default();
    let total = documents
        .get("totalCount")
        .and_then(scalar_value)
        .unwrap_or_else(|| "0".into());

    if items.is_empty() {
        return format!("No documents found for keywords: {keywords}");
    }

    let mut lines = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let title = field_as_string(item, "title");
        let content = field_as_string(item, "content");
        let url = field_as_string(item, "url");
        lines.push(format!("{}. {title}", index + 1));
        if !content.is_empty() {
            lines.push(format!("   {content}"));
        }
        if !url.is_empty() {
            lines.push(format!("   Link: {url}"));
        }
        lines.push(String::new());
    }
    lines.push(format!("Found {} documents (total {total})", items.len()));
    lines.push("Use web_fetch tool to read full document content if needed.".into());

    lines.join("\n")
}

fn object_field<'a>(
    input: &'a JsonValue,
    field: &str,
) -> Option<&'a std::collections::BTreeMap<String, JsonValue>> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    fields.get(field).and_then(as_object)
}

fn field_as_string(input: &JsonValue, field: &str) -> String {
    let JsonValue::Object(fields) = input else {
        return String::new();
    };
    fields.get(field).and_then(scalar_value).unwrap_or_default()
}

fn scalar_value(value: &JsonValue) -> Option<String> {
    match value {
        JsonValue::String(value) | JsonValue::Number(value) => Some(value.clone()),
        _ => None,
    }
}

fn as_object(value: &JsonValue) -> Option<&std::collections::BTreeMap<String, JsonValue>> {
    match value {
        JsonValue::Object(fields) => Some(fields),
        _ => None,
    }
}

fn as_array(value: &JsonValue) -> Option<&Vec<JsonValue>> {
    match value {
        JsonValue::Array(values) => Some(values),
        _ => None,
    }
}
