use iac_code_protocol::json::{self, JsonValue};

use crate::signing::{
    agent_card_signature_jwks_url, verify_agent_card_dict, AgentCardVerificationOptions,
};

use super::http::fetch_json;
use super::{object_field, A2ADiscoverOptions};

pub(super) fn discover_agent_card_with_client(
    client: &reqwest::blocking::Client,
    options: &A2ADiscoverOptions<'_>,
) -> Result<JsonValue, String> {
    let card_url = format!(
        "{}/.well-known/agent-card.json",
        options.base_url.trim_end_matches('/')
    );
    let card = fetch_json(client, &card_url, options.auth.as_ref())?;
    if !matches!(card, JsonValue::Object(_)) {
        return Err("A2A Agent Card response must be a JSON object".to_owned());
    }

    let should_verify = options.require_card_signature
        || options
            .verification_secret
            .as_ref()
            .is_some_and(|value| !value.is_empty())
        || options
            .verification_jwks_url
            .as_ref()
            .is_some_and(|value| !value.is_empty());
    if should_verify {
        let remote_jwks_url = options
            .verification_jwks_url
            .as_deref()
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
            .or_else(|| agent_card_signature_jwks_url(&card));
        let remote_jwks = match remote_jwks_url {
            Some(url) => Some(fetch_json(client, &url, None)?),
            None => None,
        };
        if let Some(jwks) = &remote_jwks {
            if !matches!(jwks, JsonValue::Object(_)) {
                return Err("A2A JWKS response must be a JSON object".to_owned());
            }
        }
        let result = verify_agent_card_dict(
            &card,
            AgentCardVerificationOptions {
                secret: options
                    .verification_secret
                    .clone()
                    .filter(|value| !value.is_empty()),
                jwks: remote_jwks,
                require_signature: options.require_card_signature,
                ..AgentCardVerificationOptions::default()
            },
        );
        if !result.valid {
            return Err(format!(
                "A2A Agent Card verification failed: {}",
                result.message()
            ));
        }
    }

    Ok(card)
}

pub fn merge_jwks(values: &[Option<&JsonValue>]) -> Option<JsonValue> {
    let mut keys = Vec::new();
    for value in values.iter().flatten() {
        let Some(JsonValue::Array(jwks_keys)) = object_field(value, "keys") else {
            continue;
        };
        keys.extend(jwks_keys.iter().cloned());
    }
    if keys.is_empty() {
        None
    } else {
        Some(json::object([("keys", JsonValue::Array(keys))]))
    }
}
