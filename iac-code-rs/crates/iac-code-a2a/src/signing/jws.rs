use base64::engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD};
use base64::Engine;
use iac_code_protocol::json::{self, JsonValue};

pub(super) fn canonicalize_agent_card(card: &JsonValue) -> Vec<u8> {
    without_signature(card).to_compact_json().into_bytes()
}

pub(super) fn without_signature(card: &JsonValue) -> JsonValue {
    let JsonValue::Object(object) = card else {
        return card.clone();
    };
    let mut data = object.clone();
    data.remove("signatures");
    if let Some(JsonValue::Object(metadata)) = data.get_mut("metadata") {
        metadata.remove("iac_code_signature");
        if metadata.is_empty() {
            data.remove("metadata");
        }
    }
    JsonValue::Object(data)
}

pub(super) fn first_signature(card: &JsonValue) -> Option<&JsonValue> {
    let JsonValue::Object(object) = card else {
        return None;
    };
    let Some(JsonValue::Array(signatures)) = object.get("signatures") else {
        return None;
    };
    match signatures.first() {
        Some(JsonValue::Object(_)) => signatures.first(),
        _ => None,
    }
}

pub(super) fn decode_protected_header(value: &str) -> Option<JsonValue> {
    let decoded = base64url_decode(value).ok()?;
    let text = std::str::from_utf8(&decoded).ok()?;
    match json::parse(text).ok()? {
        JsonValue::Object(_) => json::parse(text).ok(),
        _ => None,
    }
}

pub(super) fn string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key).and_then(json_string)
}

pub(super) fn json_string(value: &JsonValue) -> Option<&str> {
    match value {
        JsonValue::String(value) => Some(value.as_str()),
        _ => None,
    }
}

pub(super) fn base64url_encode(bytes: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(bytes)
}

pub(super) fn base64url_decode(value: &str) -> Result<Vec<u8>, base64::DecodeError> {
    URL_SAFE_NO_PAD.decode(value).or_else(|_| {
        let mut padded = value.to_owned();
        while !padded.len().is_multiple_of(4) {
            padded.push('=');
        }
        URL_SAFE.decode(padded)
    })
}
