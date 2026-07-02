use std::collections::BTreeMap;

use iac_code_a2a::signing::{
    agent_card_signature_jwks_url, canonicalize_agent_card, sign_agent_card_dict,
    verify_agent_card_dict, AgentCardVerificationOptions, ASYMMETRIC_SIGNATURE_ALGORITHM,
    SIGNATURE_ALGORITHM,
};
use iac_code_protocol::json::{self, JsonValue};

const SECRET: &str = "ssssssssssssssssssssssssssssssss";
const OTHER_SECRET: &str = "dddddddddddddddddddddddddddddddd";
const PYTHON_HS_SIGNED: &str = r#"{"name":"iac-code","signatures":[{"protected":"eyJhbGciOiJIUzI1NiIsImprdSI6bnVsbCwia2lkIjoibG9jYWwiLCJ0eXAiOiJKT1NFIn0","signature":"VmhvrQ5bMMZkeBI57zEFzLsA1WWsU8FyY9YnXPXaXc0"}],"version":"1"}"#;
const PYTHON_RSA_SIGNED: &str = r#"{"name":"iac-code","signatures":[{"protected":"eyJhbGciOiJSUzI1NiIsImprdSI6Imh0dHBzOi8vYWdlbnQuZXhhbXBsZS8ud2VsbC1rbm93bi9qd2tzLmpzb24iLCJraWQiOiJyc2EtY3VycmVudCIsInR5cCI6IkpPU0UifQ","signature":"Xj-L5otCDsvY1DHeXEDia6nEgtgkldFPZxvPAhppWmjcC9ZsOZCvX1B2r-fGzDBXWOQ7lYJa5z8Iyb3uc6fJQTdSaNCU17nKIiusPPi-EwrECIlcZAhbpQ8BEItij2VLtJMDtAmyTaR59E6hpD6r-KOPGgDvy0lgcFSAq_w1eeZbXfQ_85-R0cuwC_iYWc3dVY6LO2G7jecNTuHrd-f3WjMI6sy8tqmXqItNjpvCjTIgotyFYLOAnhxcOnQ7ym_-tY1Akc3izpcypz1MZ3weibX9vmqJlemjdUoqWTm5jhhDPUM9Rg0ktb-BGLXkZkt8Qqk1IwYMWHIDgaocK6TmPQ"}]}"#;
const PYTHON_RSA_JWK: &str = r#"{"alg":"RS256","e":"AQAB","kid":"rsa-current","kty":"RSA","n":"vnWxYPyQc31tkwe8vbsmi9EM-mNK4E9SVsn4GLhzzwR7jRPFlepDexwYbAzUcD6iEfv9Wk180RStEbBxwfzy_trm98KTf5I-QdVfP3HE6CwCgcFOYc0i29vFvJgfp_o2CVzfc_zAWbhN34YfZ1IKhuekx5TabaC61qFOP0k1JAs5kimc027itE1UouCBDWUTm5ixGIKp7YFfeMb4YAZngM7wADb6s_huoorJ2SoRG6Fm3TsWUq7ZZV5NmEBEiatvgfC3-I5cLyvwx34zk38fy1xpIIYyKvNvo5u0MhtNqC79X6CxxLA8pgmMThpwqDK95tzziL6pwEXtG2HpPHrhuQ","use":"sig"}"#;

#[test]
fn canonicalize_agent_card_is_stable_and_strips_signature_metadata() {
    let left = json::object([
        ("name", json::string("iac-code")),
        (
            "skills",
            json::array([json::object([("id", json::string("iac"))])]),
        ),
        ("version", json::string("1")),
        (
            "metadata",
            json::object([("iac_code_signature", json::string("stale"))]),
        ),
        (
            "signatures",
            json::array([json::object([("signature", json::string("old"))])]),
        ),
    ]);
    let right = json::object([
        ("version", json::string("1")),
        (
            "skills",
            json::array([json::object([("id", json::string("iac"))])]),
        ),
        ("name", json::string("iac-code")),
    ]);

    assert_eq!(
        canonicalize_agent_card(&left),
        canonicalize_agent_card(&right)
    );
    assert_eq!(
        canonicalize_agent_card(&left),
        br#"{"name":"iac-code","skills":[{"id":"iac"}],"version":"1"}"#.to_vec()
    );
}

#[test]
fn sign_agent_card_matches_python_hs256_jws_shape() {
    let card = json::object([
        ("name", json::string("iac-code")),
        ("version", json::string("1")),
    ]);

    let signed = sign_agent_card_dict(&card, SECRET, "local");

    assert_eq!(signed.to_compact_json(), PYTHON_HS_SIGNED);
    assert_eq!(
        string_at(&signed, &["signatures", "0", "protected"]),
        "eyJhbGciOiJIUzI1NiIsImprdSI6bnVsbCwia2lkIjoibG9jYWwiLCJ0eXAiOiJKT1NFIn0"
    );
    assert!(missing_at(&signed, &["signatures", "0", "header"]));
}

#[test]
fn verify_agent_card_accepts_python_hs256_and_reports_python_reasons() {
    let signed = parse(PYTHON_HS_SIGNED);

    let valid = verify_agent_card_dict(&signed, AgentCardVerificationOptions::secret(SECRET));
    assert!(valid.valid);
    assert_eq!(valid.reason, "valid");
    assert_eq!(valid.key_id.as_deref(), Some("local"));

    let mismatch =
        verify_agent_card_dict(&signed, AgentCardVerificationOptions::secret(OTHER_SECRET));
    assert!(!mismatch.valid);
    assert_eq!(mismatch.reason, "signature-mismatch");
    assert_eq!(mismatch.message(), "signature-mismatch: kid=local");

    let unknown = verify_agent_card_dict(
        &signed,
        AgentCardVerificationOptions {
            secrets: BTreeMap::from([("other".to_owned(), SECRET.to_owned())]),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );
    assert!(!unknown.valid);
    assert_eq!(unknown.reason, "unknown-key");
    assert_eq!(unknown.key_id.as_deref(), Some("local"));
}

#[test]
fn verify_agent_card_handles_unsigned_strict_and_unsupported_algorithm_like_python() {
    let unsigned = json::object([("name", json::string("unsigned"))]);

    let allowed = verify_agent_card_dict(&unsigned, AgentCardVerificationOptions::secret(SECRET));
    assert!(allowed.valid);
    assert_eq!(allowed.reason, "unsigned");

    let strict = verify_agent_card_dict(
        &unsigned,
        AgentCardVerificationOptions {
            secret: Some(SECRET.to_owned()),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );
    assert!(!strict.valid);
    assert_eq!(strict.reason, "missing-signature");

    let mut unsupported = parse(PYTHON_HS_SIGNED);
    set_string_at(
        &mut unsupported,
        &["signatures", "0", "protected"],
        "eyJhbGciOiJFUzI1NiIsImprdSI6bnVsbCwia2lkIjoibG9jYWwiLCJ0eXAiOiJKT1NFIn0",
    );
    let result = verify_agent_card_dict(&unsupported, AgentCardVerificationOptions::secret(SECRET));
    assert!(!result.valid);
    assert_eq!(result.reason, "unsupported-algorithm");
    assert_eq!(result.message(), "unsupported-algorithm: alg=ES256");
}

#[test]
fn verify_agent_card_selects_hmac_secret_by_kid_and_oct_jwks() {
    let signed = parse(PYTHON_HS_SIGNED);

    let selected = verify_agent_card_dict(
        &signed,
        AgentCardVerificationOptions {
            secrets: BTreeMap::from([
                ("old".to_owned(), OTHER_SECRET.to_owned()),
                ("local".to_owned(), SECRET.to_owned()),
            ]),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );
    assert!(selected.valid);
    assert_eq!(selected.key_id.as_deref(), Some("local"));

    let jwks = json::object([(
        "keys",
        json::array([json::object([
            ("kty", json::string("oct")),
            ("kid", json::string("local")),
            (
                "k",
                json::string("c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M"),
            ),
        ])]),
    )]);
    let from_jwks = verify_agent_card_dict(
        &signed,
        AgentCardVerificationOptions {
            jwks: Some(jwks),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );
    assert!(from_jwks.valid);
    assert_eq!(from_jwks.key_id.as_deref(), Some("local"));
}

#[test]
fn verify_agent_card_accepts_python_rs256_with_rsa_jwks_and_extracts_jku() {
    let signed = parse(PYTHON_RSA_SIGNED);
    let jwks = json::object([("keys", json::array([parse(PYTHON_RSA_JWK)]))]);

    let result = verify_agent_card_dict(
        &signed,
        AgentCardVerificationOptions {
            jwks: Some(jwks),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );

    assert!(result.valid);
    assert_eq!(result.key_id.as_deref(), Some("rsa-current"));
    assert_eq!(
        agent_card_signature_jwks_url(&signed).as_deref(),
        Some("https://agent.example/.well-known/jwks.json")
    );
}

#[test]
fn verify_agent_card_rejects_ambiguous_missing_kid_with_multiple_keys() {
    let mut signed = parse(PYTHON_HS_SIGNED);
    set_string_at(
        &mut signed,
        &["signatures", "0", "protected"],
        "eyJhbGciOiJIUzI1NiIsImprdSI6bnVsbCwidHlwIjoiSk9TRSJ9",
    );

    let result = verify_agent_card_dict(
        &signed,
        AgentCardVerificationOptions {
            secrets: BTreeMap::from([
                ("one".to_owned(), SECRET.to_owned()),
                ("two".to_owned(), OTHER_SECRET.to_owned()),
            ]),
            require_signature: true,
            ..AgentCardVerificationOptions::default()
        },
    );

    assert!(!result.valid);
    assert_eq!(result.reason, "ambiguous-key");
}

#[test]
fn signing_constants_match_python_module() {
    assert_eq!(SIGNATURE_ALGORITHM, "HS256");
    assert_eq!(ASYMMETRIC_SIGNATURE_ALGORITHM, "RS256");
}

fn parse(text: &str) -> JsonValue {
    json::parse(text).expect("valid json fixture")
}

fn string_at(value: &JsonValue, path: &[&str]) -> String {
    match at(value, path) {
        Some(JsonValue::String(value)) => value.clone(),
        other => panic!("expected string at {path:?}, got {other:?}"),
    }
}

fn missing_at(value: &JsonValue, path: &[&str]) -> bool {
    at(value, path).is_none()
}

fn set_string_at(value: &mut JsonValue, path: &[&str], replacement: &str) {
    let Some((last, parents)) = path.split_last() else {
        panic!("path must not be empty");
    };
    let mut current = value;
    for segment in parents {
        current = match current {
            JsonValue::Object(object) => object
                .get_mut(*segment)
                .unwrap_or_else(|| panic!("missing object key {segment:?}")),
            JsonValue::Array(values) => values
                .get_mut(segment.parse::<usize>().expect("array index"))
                .unwrap_or_else(|| panic!("missing array index {segment:?}")),
            other => panic!("cannot descend into {other:?}"),
        };
    }
    match current {
        JsonValue::Object(object) => {
            object.insert((*last).to_owned(), json::string(replacement));
        }
        JsonValue::Array(values) => {
            values[(*last).parse::<usize>().expect("array index")] = json::string(replacement);
        }
        other => panic!("cannot set value on {other:?}"),
    }
}

fn at<'a>(mut value: &'a JsonValue, path: &[&str]) -> Option<&'a JsonValue> {
    for segment in path {
        value = match value {
            JsonValue::Object(object) => object.get(*segment)?,
            JsonValue::Array(values) => values.get(segment.parse::<usize>().ok()?)?,
            _ => return None,
        };
    }
    Some(value)
}
