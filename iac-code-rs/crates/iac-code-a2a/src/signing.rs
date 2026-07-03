use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};
use ring::hmac;

mod jwk;
mod jws;

use jwk::{select_verification_key, verify_signature};
use jws::{
    base64url_decode, base64url_encode, decode_protected_header, first_signature, string_field,
    without_signature,
};

pub const SIGNATURE_ALGORITHM: &str = "HS256";
pub const ASYMMETRIC_SIGNATURE_ALGORITHM: &str = "RS256";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentCardSignature {
    pub valid: bool,
    pub reason: String,
    pub key_id: Option<String>,
    pub detail: String,
}

impl AgentCardSignature {
    pub fn message(&self) -> String {
        if !self.detail.is_empty() {
            return format!("{}: {}", self.reason, self.detail);
        }
        if let Some(key_id) = &self.key_id {
            if matches!(self.reason.as_str(), "unknown-key" | "signature-mismatch") {
                return format!("{}: kid={key_id}", self.reason);
            }
        }
        self.reason.clone()
    }

    fn valid(key_id: Option<String>) -> Self {
        Self {
            valid: true,
            reason: "valid".to_owned(),
            key_id,
            detail: String::new(),
        }
    }

    fn invalid(reason: &str, key_id: Option<String>) -> Self {
        Self {
            valid: false,
            reason: reason.to_owned(),
            key_id,
            detail: String::new(),
        }
    }

    fn invalid_detail(reason: &str, detail: String) -> Self {
        Self {
            valid: false,
            reason: reason.to_owned(),
            key_id: None,
            detail,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AgentCardVerificationOptions {
    pub secret: Option<String>,
    pub secrets: BTreeMap<String, String>,
    pub jwks: Option<JsonValue>,
    pub require_signature: bool,
}

impl AgentCardVerificationOptions {
    pub fn secret(secret: impl Into<String>) -> Self {
        Self {
            secret: Some(secret.into()),
            ..Self::default()
        }
    }
}

pub fn canonicalize_agent_card(card: &JsonValue) -> Vec<u8> {
    jws::canonicalize_agent_card(card)
}

pub fn sign_agent_card_dict(card: &JsonValue, secret: &str, key_id: &str) -> JsonValue {
    let mut signed = without_signature(card);
    let protected_header = json::object([
        ("alg", json::string(SIGNATURE_ALGORITHM)),
        ("jku", JsonValue::Null),
        ("kid", json::string(key_id)),
        ("typ", json::string("JOSE")),
    ]);
    let protected = base64url_encode(protected_header.to_compact_json().as_bytes());
    let payload = base64url_encode(&canonicalize_agent_card(&signed));
    let signing_input = format!("{protected}.{payload}");
    let signature = hmac::sign(
        &hmac::Key::new(hmac::HMAC_SHA256, secret.as_bytes()),
        signing_input.as_bytes(),
    );
    let signature = base64url_encode(signature.as_ref());

    if let JsonValue::Object(object) = &mut signed {
        object.insert(
            "signatures".to_owned(),
            json::array([json::object([
                ("protected", json::string(protected)),
                ("signature", json::string(signature)),
            ])]),
        );
    }
    signed
}

pub fn verify_agent_card_dict(
    card: &JsonValue,
    options: AgentCardVerificationOptions,
) -> AgentCardSignature {
    let Some(signature_data) = first_signature(card) else {
        if options.require_signature {
            return AgentCardSignature::invalid("missing-signature", None);
        }
        return AgentCardSignature {
            valid: true,
            reason: "unsigned".to_owned(),
            key_id: None,
            detail: String::new(),
        };
    };

    let Some(protected_header) =
        string_field(signature_data, "protected").and_then(decode_protected_header)
    else {
        return AgentCardSignature::invalid("malformed-signature", None);
    };
    let algorithm = string_field(&protected_header, "alg");
    let Some(algorithm) = algorithm else {
        return AgentCardSignature::invalid_detail(
            "unsupported-algorithm",
            "alg=<missing>".to_owned(),
        );
    };
    if !matches!(
        algorithm,
        SIGNATURE_ALGORITHM | ASYMMETRIC_SIGNATURE_ALGORITHM
    ) {
        return AgentCardSignature::invalid_detail(
            "unsupported-algorithm",
            format!("alg={algorithm}"),
        );
    }
    if string_field(signature_data, "signature").is_none() {
        return AgentCardSignature::invalid("malformed-signature", None);
    }
    let key_id = string_field(&protected_header, "kid").map(str::to_owned);
    let verification_key = match select_verification_key(&options, key_id.as_deref(), algorithm) {
        Ok(key) => key,
        Err(result) => return result,
    };

    let protected = string_field(signature_data, "protected").unwrap_or_default();
    let payload = base64url_encode(&canonicalize_agent_card(card));
    let signing_input = format!("{protected}.{payload}");
    let Some(signature_value) = string_field(signature_data, "signature") else {
        return AgentCardSignature::invalid("malformed-signature", key_id);
    };
    let Ok(signature_bytes) = base64url_decode(signature_value) else {
        return AgentCardSignature::invalid("malformed-signature", key_id);
    };

    match verify_signature(
        &verification_key,
        algorithm,
        signing_input.as_bytes(),
        &signature_bytes,
    ) {
        Ok(()) => AgentCardSignature::valid(key_id),
        Err(()) => AgentCardSignature::invalid("signature-mismatch", key_id),
    }
}

pub fn agent_card_signature_jwks_url(card: &JsonValue) -> Option<String> {
    let signature_data = first_signature(card)?;
    let protected_header =
        string_field(signature_data, "protected").and_then(decode_protected_header)?;
    string_field(&protected_header, "jku")
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}
