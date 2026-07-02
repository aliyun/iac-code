use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;
use ring::{hmac, signature};

use super::jws::{base64url_decode, json_string, string_field};
use super::{
    AgentCardSignature, AgentCardVerificationOptions, ASYMMETRIC_SIGNATURE_ALGORITHM,
    SIGNATURE_ALGORITHM,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum VerificationKey {
    Hmac(String),
    Rsa { n: Vec<u8>, e: Vec<u8> },
}

pub(super) fn select_verification_key(
    options: &AgentCardVerificationOptions,
    key_id: Option<&str>,
    algorithm: &str,
) -> Result<VerificationKey, AgentCardSignature> {
    let mut key_map = BTreeMap::new();
    for (kid, secret) in &options.secrets {
        key_map.insert(kid.clone(), VerificationKey::Hmac(secret.clone()));
    }
    for (kid, key) in jwks_verification_keys(options.jwks.as_ref(), algorithm) {
        key_map.insert(kid, key);
    }

    if key_map.is_empty() {
        let Some(secret) = &options.secret else {
            return Err(AgentCardSignature::invalid(
                "missing-key",
                key_id.map(str::to_owned),
            ));
        };
        return Ok(VerificationKey::Hmac(secret.clone()));
    }

    if let Some(key_id) = key_id {
        return key_map
            .get(key_id)
            .cloned()
            .ok_or_else(|| AgentCardSignature::invalid("unknown-key", Some(key_id.to_owned())));
    }

    if key_map.len() == 1 {
        return Ok(key_map.into_values().next().expect("single key"));
    }
    Err(AgentCardSignature::invalid("ambiguous-key", None))
}

fn jwks_verification_keys(
    jwks: Option<&JsonValue>,
    algorithm: &str,
) -> BTreeMap<String, VerificationKey> {
    let mut decoded = BTreeMap::new();
    let Some(JsonValue::Object(jwks)) = jwks else {
        return decoded;
    };
    let Some(JsonValue::Array(keys)) = jwks.get("keys") else {
        return decoded;
    };

    for item in keys {
        let JsonValue::Object(key) = item else {
            continue;
        };
        let Some(kid) = string_field(item, "kid") else {
            continue;
        };
        if let Some(jwk_alg) = string_field(item, "alg") {
            if jwk_alg != algorithm {
                continue;
            }
        }
        match string_field(item, "kty") {
            Some("oct") if algorithm == SIGNATURE_ALGORITHM => {
                let Some(key_value) = string_field(item, "k") else {
                    continue;
                };
                let Ok(raw) = base64url_decode(key_value) else {
                    continue;
                };
                let Ok(secret) = String::from_utf8(raw) else {
                    continue;
                };
                decoded.insert(kid.to_owned(), VerificationKey::Hmac(secret));
            }
            Some("RSA") if algorithm == ASYMMETRIC_SIGNATURE_ALGORITHM => {
                let Some(n_value) = key.get("n").and_then(json_string) else {
                    continue;
                };
                let Some(e_value) = key.get("e").and_then(json_string) else {
                    continue;
                };
                let Ok(n) = base64url_decode(n_value) else {
                    continue;
                };
                let Ok(e) = base64url_decode(e_value) else {
                    continue;
                };
                decoded.insert(kid.to_owned(), VerificationKey::Rsa { n, e });
            }
            _ => {}
        }
    }
    decoded
}

pub(super) fn verify_signature(
    key: &VerificationKey,
    algorithm: &str,
    signing_input: &[u8],
    signature_bytes: &[u8],
) -> Result<(), ()> {
    match (algorithm, key) {
        (SIGNATURE_ALGORITHM, VerificationKey::Hmac(secret)) => hmac::verify(
            &hmac::Key::new(hmac::HMAC_SHA256, secret.as_bytes()),
            signing_input,
            signature_bytes,
        )
        .map_err(|_| ()),
        (ASYMMETRIC_SIGNATURE_ALGORITHM, VerificationKey::Rsa { n, e }) => {
            signature::RsaPublicKeyComponents {
                n: n.as_slice(),
                e: e.as_slice(),
            }
            .verify(
                &signature::RSA_PKCS1_2048_8192_SHA256,
                signing_input,
                signature_bytes,
            )
            .map_err(|_| ())
        }
        _ => Err(()),
    }
}
