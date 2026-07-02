use std::collections::BTreeMap;
use std::time::Duration;

use crate::cli_i18n::tr;
use crate::raw_auth_oauth_types::{
    RawAuthAliyunOAuthSite, RawAuthAliyunOAuthSts, RawAuthAliyunOAuthToken,
};
use crate::raw_auth_oauth_utils::{
    raw_auth_current_epoch, raw_auth_first_required_json_epoch,
    raw_auth_first_required_json_string, raw_auth_form_encode, raw_auth_optional_json_i64,
    raw_auth_required_json_i64, raw_auth_required_json_string,
};

pub(super) fn raw_auth_aliyun_oauth_exchange_code_for_token(
    site: &RawAuthAliyunOAuthSite,
    code: &str,
    redirect_uri: &str,
    code_verifier: &str,
) -> Result<RawAuthAliyunOAuthToken, String> {
    let mut form = BTreeMap::new();
    form.insert("grant_type".to_owned(), "authorization_code".to_owned());
    form.insert("code".to_owned(), code.to_owned());
    form.insert("client_id".to_owned(), site.client_id.to_owned());
    form.insert("redirect_uri".to_owned(), redirect_uri.to_owned());
    form.insert("code_verifier".to_owned(), code_verifier.to_owned());
    let data = raw_auth_oauth_post_json(
        "exchange authorization code for token",
        &[code, code_verifier],
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|error| {
                format!("exchange authorization code for token request failed: {error}")
            })?
            .post(format!(
                "{}/v1/token",
                site.oauth_base_url.trim_end_matches('/')
            ))
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/x-www-form-urlencoded",
            )
            .body(raw_auth_form_encode(&form)),
    )?;
    let now = raw_auth_current_epoch();
    let access_token = raw_auth_required_json_string(
        &data,
        "access_token",
        "exchange authorization code for token",
    )?;
    let refresh_token = raw_auth_required_json_string(
        &data,
        "refresh_token",
        "exchange authorization code for token",
    )?;
    let expires_in =
        raw_auth_required_json_i64(&data, "expires_in", "exchange authorization code for token")?;
    let refresh_token_expire = raw_auth_optional_json_i64(&data, "refresh_expires_in")
        .map(|expires_in| now + expires_in)
        .unwrap_or_default();
    Ok(RawAuthAliyunOAuthToken {
        access_token,
        refresh_token,
        access_token_expire: now + expires_in,
        refresh_token_expire,
    })
}

pub(super) fn raw_auth_aliyun_oauth_exchange_access_token_for_sts(
    site: &RawAuthAliyunOAuthSite,
    access_token: &str,
) -> Result<RawAuthAliyunOAuthSts, String> {
    let data = raw_auth_oauth_post_json(
        "exchange access token for STS",
        &[access_token],
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|error| format!("exchange access token for STS request failed: {error}"))?
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
    Ok(RawAuthAliyunOAuthSts {
        access_key_id: raw_auth_first_required_json_string(
            &data,
            &["accessKeyId", "AccessKeyId"],
            "accessKeyId",
            "STS exchange response",
        )?,
        access_key_secret: raw_auth_first_required_json_string(
            &data,
            &["accessKeySecret", "AccessKeySecret"],
            "accessKeySecret",
            "STS exchange response",
        )?,
        sts_token: raw_auth_first_required_json_string(
            &data,
            &["securityToken", "SecurityToken"],
            "securityToken",
            "STS exchange response",
        )?,
        sts_expiration: raw_auth_first_required_json_epoch(
            &data,
            &["expiration", "Expiration"],
            "expiration",
            "STS exchange response",
        )?,
    })
}

fn raw_auth_oauth_post_json(
    operation: &str,
    sensitive_values: &[&str],
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
        return Err(raw_auth_oauth_status_error(
            operation,
            status.as_u16(),
            &text,
            sensitive_values,
        ));
    }
    match serde_json::from_str::<serde_json::Value>(&text)
        .map_err(|_| format!("{operation} response was not valid JSON"))?
    {
        serde_json::Value::Object(map) => Ok(map),
        _ => Err(format!("{operation} response JSON was not an object")),
    }
}

fn raw_auth_oauth_status_error(
    operation: &str,
    status: u16,
    text: &str,
    sensitive_values: &[&str],
) -> String {
    let data = serde_json::from_str::<serde_json::Value>(text).unwrap_or(serde_json::Value::Null);
    let error = data
        .get("error")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let mut description = data
        .get("error_description")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .to_owned();
    for sensitive_value in sensitive_values {
        if !sensitive_value.is_empty() {
            description = description.replace(sensitive_value, "[REDACTED]");
        }
    }
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
    if matches!(
        error,
        "invalid_grant" | "invalid_client" | "unauthorized_client" | "invalid_token"
    ) {
        let hint = tr("Run /auth and choose OAuth Login (Browser).");
        format!("{message} {hint}")
    } else {
        message
    }
}
