use std::collections::BTreeMap;
use std::time::Duration;

use super::encoding::form_encode;

#[derive(Clone, Debug)]
pub(super) struct OAuthSite {
    client_id: &'static str,
    oauth_base_url: String,
}

#[derive(Clone, Debug)]
pub(super) struct OAuthToken {
    pub(super) access_token: String,
    pub(super) refresh_token: String,
    pub(super) access_token_expire: i64,
    pub(super) refresh_token_expire: i64,
}

#[derive(Clone, Debug)]
pub(super) struct OAuthStsCredentials {
    pub(super) access_key_id: String,
    pub(super) access_key_secret: String,
    pub(super) sts_token: String,
    pub(super) sts_expiration: i64,
}

impl OAuthSite {
    pub(super) fn resolve(
        site_type: &str,
        base_url_override: Option<&str>,
    ) -> Result<Self, String> {
        let normalized = site_type.trim().to_ascii_uppercase();
        let (client_id, default_base_url) = match normalized.as_str() {
            "CN" => ("4038181954557748008", "https://oauth.aliyun.com"),
            "INTL" => ("4103531455503354461", "https://oauth.alibabacloud.com"),
            _ => return Err(format!("Unknown Aliyun OAuth site: {site_type}")),
        };
        Ok(Self {
            client_id,
            oauth_base_url: base_url_override.unwrap_or(default_base_url).to_owned(),
        })
    }
}

pub(super) fn refresh_oauth_access_token(
    site: &OAuthSite,
    refresh_token: &str,
    now: i64,
) -> Result<OAuthToken, String> {
    let mut form = BTreeMap::new();
    form.insert("grant_type".to_owned(), "refresh_token".to_owned());
    form.insert("refresh_token".to_owned(), refresh_token.to_owned());
    form.insert("client_id".to_owned(), site.client_id.to_owned());
    let data = post_oauth_json(
        "refresh access token",
        Some(refresh_token),
        oauth_http_client("refresh access token")?
            .post(format!(
                "{}/v1/token",
                site.oauth_base_url.trim_end_matches('/')
            ))
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/x-www-form-urlencoded",
            )
            .body(form_encode(&form)),
    )?;

    let access_token = required_json_string(&data, "access_token", "refresh access token")?;
    let refresh_token =
        optional_json_string(&data, "refresh_token").unwrap_or_else(|| refresh_token.to_owned());
    let expires_in = required_json_i64(&data, "expires_in", "refresh access token")?;
    let refresh_token_expire = optional_json_i64(&data, "refresh_expires_in")
        .map(|expires_in| now + expires_in)
        .unwrap_or_default();

    Ok(OAuthToken {
        access_token,
        refresh_token,
        access_token_expire: now + expires_in,
        refresh_token_expire,
    })
}

pub(super) fn exchange_oauth_access_token_for_sts(
    site: &OAuthSite,
    access_token: &str,
) -> Result<OAuthStsCredentials, String> {
    let data = post_oauth_json(
        "exchange access token for STS",
        Some(access_token),
        oauth_http_client("exchange access token for STS")?
            .post(format!(
                "{}/v1/exchange",
                site.oauth_base_url.trim_end_matches('/')
            ))
            .header(
                reqwest::header::AUTHORIZATION,
                format!("Bearer {access_token}"),
            )
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body("{}"),
    )?;

    let access_key_id = first_required_json_string(
        &data,
        &["accessKeyId", "AccessKeyId"],
        "accessKeyId",
        "STS exchange response",
    )?;
    let access_key_secret = first_required_json_string(
        &data,
        &["accessKeySecret", "AccessKeySecret"],
        "accessKeySecret",
        "STS exchange response",
    )?;
    let sts_token = first_required_json_string(
        &data,
        &["securityToken", "SecurityToken"],
        "securityToken",
        "STS exchange response",
    )?;
    let sts_expiration = first_required_json_epoch(
        &data,
        &["expiration", "Expiration"],
        "expiration",
        "STS exchange response",
    )?;

    Ok(OAuthStsCredentials {
        access_key_id,
        access_key_secret,
        sts_token,
        sts_expiration,
    })
}

fn oauth_http_client(operation: &str) -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|error| format!("{operation} request failed: {error}"))
}

fn post_oauth_json(
    operation: &str,
    sensitive_value: Option<&str>,
    request: reqwest::blocking::RequestBuilder,
) -> Result<serde_json::Map<String, serde_json::Value>, String> {
    let response = request
        .send()
        .map_err(|error| format!("{operation} request failed: {error}"))?;
    let status = response.status();
    let text = response
        .text()
        .map_err(|error| format!("{operation} request failed: {error}"))?;
    if !status.is_success() {
        return Err(oauth_status_error(
            operation,
            status.as_u16(),
            &text,
            sensitive_value,
        ));
    }
    let data = serde_json::from_str::<serde_json::Value>(&text)
        .map_err(|_| format!("{operation} response was not valid JSON"))?;
    match data {
        serde_json::Value::Object(map) => Ok(map),
        _ => Err(format!("{operation} response JSON was not an object")),
    }
}

fn oauth_status_error(
    operation: &str,
    status: u16,
    text: &str,
    sensitive_value: Option<&str>,
) -> String {
    let data = serde_json::from_str::<serde_json::Value>(text).unwrap_or(serde_json::Value::Null);
    let error = data
        .get("error")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let description = data
        .get("error_description")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let description = match sensitive_value.filter(|value| !value.is_empty()) {
        Some(value) => description.replace(value, "[REDACTED]"),
        None => description.to_owned(),
    };

    let mut parts = Vec::new();
    if !error.is_empty() {
        parts.push(format!("error={error}"));
    }
    if !description.is_empty() {
        parts.push(format!("error_description={description}"));
    }
    let message = if parts.is_empty() {
        format!("{operation} failed with status {status}")
    } else {
        format!(
            "{operation} failed with status {status}: {}",
            parts.join(", ")
        )
    };
    if is_permanent_oauth_error(error) {
        oauth_relogin_message(&message)
    } else {
        message
    }
}

fn is_permanent_oauth_error(error: &str) -> bool {
    matches!(
        error,
        "invalid_grant" | "invalid_client" | "unauthorized_client" | "invalid_token"
    )
}

fn required_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    operation: &str,
) -> Result<String, String> {
    first_required_json_string(data, &[field], field, operation)
}

fn first_required_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    fields: &[&str],
    display_field: &str,
    operation: &str,
) -> Result<String, String> {
    fields
        .iter()
        .find_map(|field| optional_json_string(data, field))
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{operation} missing required field(s): {display_field}"))
}

fn optional_json_string(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> Option<String> {
    match data.get(field)? {
        serde_json::Value::String(value) => Some(value.clone()),
        serde_json::Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn required_json_i64(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    operation: &str,
) -> Result<i64, String> {
    first_required_json_i64(data, &[field], field, operation)
}

fn first_required_json_i64(
    data: &serde_json::Map<String, serde_json::Value>,
    fields: &[&str],
    display_field: &str,
    operation: &str,
) -> Result<i64, String> {
    for field in fields {
        if let Some(value) = data.get(*field) {
            return json_i64_value(value)
                .ok_or_else(|| format!("{operation} has invalid {display_field}"));
        }
    }
    Err(format!(
        "{operation} missing required field(s): {display_field}"
    ))
}

fn first_required_json_epoch(
    data: &serde_json::Map<String, serde_json::Value>,
    fields: &[&str],
    display_field: &str,
    operation: &str,
) -> Result<i64, String> {
    for field in fields {
        if let Some(value) = data.get(*field) {
            return json_epoch_value(value)
                .ok_or_else(|| format!("{operation} has invalid {display_field}"));
        }
    }
    Err(format!(
        "{operation} missing required field(s): {display_field}"
    ))
}

fn optional_json_i64(
    data: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> Option<i64> {
    data.get(field).and_then(json_i64_value)
}

fn json_i64_value(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::Number(value) => {
            value.as_i64().or_else(|| value.as_f64().map(|v| v as i64))
        }
        serde_json::Value::String(value) => value.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn json_epoch_value(value: &serde_json::Value) -> Option<i64> {
    match value {
        serde_json::Value::String(value) => {
            let value = value.trim();
            value
                .parse::<i64>()
                .ok()
                .or_else(|| parse_iso8601_epoch(value))
        }
        _ => json_i64_value(value),
    }
}

fn parse_iso8601_epoch(value: &str) -> Option<i64> {
    let (date, time_and_zone) = value.split_once('T').or_else(|| value.split_once(' '))?;
    let mut date_parts = date.split('-');
    let year = date_parts.next()?.parse::<i32>().ok()?;
    let month = date_parts.next()?.parse::<u32>().ok()?;
    let day = date_parts.next()?.parse::<u32>().ok()?;
    if date_parts.next().is_some() || !valid_date(year, month, day) {
        return None;
    }

    let (time, offset_seconds) = parse_time_zone(time_and_zone)?;
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

    let local_epoch = days_from_civil(year, month, day) * 86_400
        + i64::from(hour) * 3_600
        + i64::from(minute) * 60
        + i64::from(second);
    Some(local_epoch - offset_seconds)
}

fn parse_time_zone(value: &str) -> Option<(&str, i64)> {
    if let Some(time) = value.strip_suffix('Z') {
        return Some((time, 0));
    }
    if let Some(index) = value.rfind(['+', '-']) {
        let time = &value[..index];
        let offset = &value[index..];
        let sign = if offset.starts_with('-') { -1 } else { 1 };
        let offset = &offset[1..];
        let (hours, minutes) = offset.split_once(':')?;
        let hours = hours.parse::<i64>().ok()?;
        let minutes = minutes.parse::<i64>().ok()?;
        if hours > 23 || minutes > 59 {
            return None;
        }
        return Some((time, sign * (hours * 3_600 + minutes * 60)));
    }
    Some((value, 0))
}

fn valid_date(year: i32, month: u32, day: u32) -> bool {
    if !(1..=12).contains(&month) {
        return false;
    }
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => return false,
    };
    (1..=max_day).contains(&day)
}

fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = year - i32::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = month as i32;
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + day as i32 - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    i64::from(era * 146_097 + day_of_era - 719_468)
}

pub(super) fn is_epoch_expired(expiration: i64, now: i64, skew_seconds: i64) -> bool {
    expiration <= 0 || expiration <= now + skew_seconds
}

pub(super) fn oauth_relogin_message(message: &str) -> String {
    let hint = "Run /auth and choose OAuth Login (Browser).";
    if message.contains(hint) {
        message.to_owned()
    } else {
        format!("{message} {hint}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso8601_epoch_parser_matches_python_oauth_expiration_basics() {
        assert_eq!(parse_iso8601_epoch("1970-01-01T00:41:40Z"), Some(2500));
        assert_eq!(parse_iso8601_epoch("1970-01-01T08:41:40+08:00"), Some(2500));
        assert_eq!(parse_iso8601_epoch("1970-01-01 00:41:40"), Some(2500));
        assert_eq!(parse_iso8601_epoch("1970-02-29T00:00:00Z"), None);
    }
}
