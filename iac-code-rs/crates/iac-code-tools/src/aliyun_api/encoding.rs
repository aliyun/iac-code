use std::collections::BTreeMap;

use percent_encoding::{percent_encode, AsciiSet, NON_ALPHANUMERIC};
use ring::{digest, hmac};

const ALIYUN_ENCODE_SET: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~');

pub(super) fn aliyun_encode(value: &str) -> String {
    percent_encode(value.as_bytes(), ALIYUN_ENCODE_SET).to_string()
}

pub(super) fn form_encode(query: &BTreeMap<String, String>) -> String {
    query
        .iter()
        .map(|(key, value)| format!("{}={}", aliyun_encode(key), aliyun_encode(value)))
        .collect::<Vec<_>>()
        .join("&")
}

pub(super) fn sha256_hex(bytes: &[u8]) -> String {
    hex_lower(digest::digest(&digest::SHA256, bytes).as_ref())
}

pub(super) fn hmac_sha256_hex(secret: &str, text: &str) -> String {
    let signature = hmac::sign(
        &hmac::Key::new(hmac::HMAC_SHA256, secret.as_bytes()),
        text.as_bytes(),
    );
    hex_lower(signature.as_ref())
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}
