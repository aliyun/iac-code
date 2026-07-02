use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;

use super::A2AAuthConfig;

pub fn headers_for_auth(config: Option<&A2AAuthConfig>) -> BTreeMap<String, String> {
    let Some(config) = config else {
        return BTreeMap::new();
    };
    let mut headers = BTreeMap::new();
    if let Some(token) = &config.bearer_token {
        if !token.is_empty() {
            headers.insert("Authorization".to_owned(), format!("Bearer {token}"));
        }
    } else if let (Some(username), Some(password)) =
        (&config.basic_username, &config.basic_password)
    {
        if !username.is_empty() && !password.is_empty() {
            headers.insert(
                "Authorization".to_owned(),
                format!(
                    "Basic {}",
                    STANDARD.encode(format!("{username}:{password}"))
                ),
            );
        }
    }
    if let Some(api_key) = &config.api_key {
        if !api_key.is_empty() {
            headers.insert(api_key_header(config).to_owned(), api_key.clone());
        }
    }
    headers
}

fn api_key_header(config: &A2AAuthConfig) -> &str {
    if config.api_key_header.is_empty() {
        "X-API-Key"
    } else {
        &config.api_key_header
    }
}
