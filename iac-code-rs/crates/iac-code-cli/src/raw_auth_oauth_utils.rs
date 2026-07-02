use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

pub(super) fn raw_auth_query_param(query: &str, key: &str) -> Option<String> {
    query.split('&').find_map(|part| {
        let (raw_key, raw_value) = part.split_once('=').unwrap_or((part, ""));
        (raw_auth_url_decode(raw_key) == key).then(|| raw_auth_url_decode(raw_value))
    })
}

pub(super) fn raw_auth_required_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    operation: &str,
) -> Result<String, String> {
    raw_auth_first_required_json_string(data, &[field], field, operation)
}

pub(super) fn raw_auth_first_required_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    fields: &[&str],
    display_field: &str,
    operation: &str,
) -> Result<String, String> {
    fields
        .iter()
        .find_map(|field| raw_auth_optional_json_string(data, field))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{operation} missing required field(s): {display_field}"))
}

fn raw_auth_optional_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> Option<String> {
    match data.get(field)? {
        serde_json::Value::String(value) => Some(value.clone()),
        serde_json::Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

pub(super) fn raw_auth_required_json_i64(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    operation: &str,
) -> Result<i64, String> {
    data.get(field)
        .and_then(raw_auth_json_i64_value)
        .ok_or_else(|| format!("{operation} has invalid {field}"))
}

pub(super) fn raw_auth_optional_json_i64(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> Option<i64> {
    data.get(field).and_then(raw_auth_json_i64_value)
}

fn raw_auth_json_i64_value(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::Number(value) => value
            .as_i64()
            .or_else(|| value.as_f64().map(|number| number as i64)),
        serde_json::Value::String(value) => value.trim().parse::<i64>().ok(),
        _ => None,
    }
}

pub(super) fn raw_auth_first_required_json_epoch(
    data: &serde_json::Map<String, serde_json::Value>,
    fields: &[&str],
    display_field: &str,
    operation: &str,
) -> Result<i64, String> {
    for field in fields {
        if let Some(value) = data.get(*field) {
            return raw_auth_json_epoch_value(value)
                .ok_or_else(|| format!("{operation} has invalid {display_field}"));
        }
    }
    Err(format!(
        "{operation} missing required field(s): {display_field}"
    ))
}

fn raw_auth_json_epoch_value(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::String(value) => {
            let value = value.trim();
            value
                .parse::<i64>()
                .ok()
                .or_else(|| raw_auth_parse_iso8601_epoch(value))
        }
        _ => raw_auth_json_i64_value(value),
    }
}

fn raw_auth_parse_iso8601_epoch(value: &str) -> Option<i64> {
    let (date, time_and_zone) = value.split_once('T').or_else(|| value.split_once(' '))?;
    let mut date_parts = date.split('-');
    let year = date_parts.next()?.parse::<i32>().ok()?;
    let month = date_parts.next()?.parse::<u32>().ok()?;
    let day = date_parts.next()?.parse::<u32>().ok()?;
    if date_parts.next().is_some() || !raw_auth_valid_date(year, month, day) {
        return None;
    }
    let (time, offset_seconds) = raw_auth_parse_time_zone(time_and_zone)?;
    let mut time_parts = time.split(':');
    let hour = time_parts.next()?.parse::<u32>().ok()?;
    let minute = time_parts.next()?.parse::<u32>().ok()?;
    let second_text = time_parts.next()?;
    if time_parts.next().is_some() {
        return None;
    }
    let second = second_text
        .split_once('.')
        .map(|(whole, _)| whole)
        .unwrap_or(second_text)
        .parse::<u32>()
        .ok()?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let local_epoch = raw_auth_days_from_civil(year, month, day) * 86_400
        + i64::from(hour) * 3_600
        + i64::from(minute) * 60
        + i64::from(second);
    Some(local_epoch - offset_seconds)
}

fn raw_auth_parse_time_zone(value: &str) -> Option<(&str, i64)> {
    if let Some(time) = value.strip_suffix('Z') {
        return Some((time, 0));
    }
    if let Some(index) = value.rfind(['+', '-']) {
        let time = &value[..index];
        let offset = &value[index..];
        let sign = if offset.starts_with('-') { -1 } else { 1 };
        let (hours, minutes) = offset[1..].split_once(':')?;
        let hours = hours.parse::<i64>().ok()?;
        let minutes = minutes.parse::<i64>().ok()?;
        if hours > 23 || minutes > 59 {
            return None;
        }
        return Some((time, sign * (hours * 3_600 + minutes * 60)));
    }
    Some((value, 0))
}

fn raw_auth_valid_date(year: i32, month: u32, day: u32) -> bool {
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if raw_auth_is_leap_year(year) => 29,
        2 => 28,
        _ => return false,
    };
    (1..=max_day).contains(&day)
}

fn raw_auth_is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn raw_auth_days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = year - i32::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = month as i32;
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + day as i32 - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    i64::from(era * 146_097 + day_of_era - 719_468)
}

pub(super) fn raw_auth_current_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

pub(super) fn raw_auth_form_encode(values: &BTreeMap<String, String>) -> String {
    values
        .iter()
        .map(|(key, value)| {
            format!(
                "{}={}",
                raw_auth_url_encode(key),
                raw_auth_url_encode(value)
            )
        })
        .collect::<Vec<_>>()
        .join("&")
}

fn raw_auth_url_encode(value: &str) -> String {
    let mut output = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            output.push(byte as char);
        } else {
            output.push_str(&format!("%{byte:02X}"));
        }
    }
    output
}

fn raw_auth_url_decode(value: &str) -> String {
    let mut bytes = Vec::with_capacity(value.len());
    let raw = value.as_bytes();
    let mut index = 0;
    while index < raw.len() {
        match raw[index] {
            b'+' => {
                bytes.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < raw.len() => {
                let hex = &value[index + 1..index + 3];
                if let Ok(byte) = u8::from_str_radix(hex, 16) {
                    bytes.push(byte);
                    index += 3;
                } else {
                    bytes.push(raw[index]);
                    index += 1;
                }
            }
            byte => {
                bytes.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&bytes).into_owned()
}
