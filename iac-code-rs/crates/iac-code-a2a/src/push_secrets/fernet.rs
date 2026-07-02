use base64::engine::general_purpose::URL_SAFE;
use base64::Engine;
use ring::rand::SecureRandom;
use ring::{hmac, rand};

use super::key_ids::current_time_seconds;
use super::{aes_128_cbc_decrypt, aes_128_cbc_encrypt, A2APushSecretError, AES_BLOCK_LEN};

const FERNET_VERSION: u8 = 0x80;
const FERNET_KEY_LEN: usize = 32;
const FERNET_SIGNING_KEY_LEN: usize = 16;
const FERNET_AES_KEY_LEN: usize = 16;
const FERNET_IV_LEN: usize = 16;
const FERNET_HMAC_LEN: usize = 32;

pub fn fernet_encrypt_at(
    fernet_key: &str,
    plaintext: &[u8],
    timestamp: u64,
    iv: [u8; FERNET_IV_LEN],
) -> Result<String, A2APushSecretError> {
    let key = FernetKey::decode(fernet_key)?;
    let ciphertext = aes_128_cbc_encrypt(&key.encryption_key, &iv, plaintext);

    let mut token = Vec::with_capacity(1 + 8 + FERNET_IV_LEN + ciphertext.len() + FERNET_HMAC_LEN);
    token.push(FERNET_VERSION);
    token.extend_from_slice(&timestamp.to_be_bytes());
    token.extend_from_slice(&iv);
    token.extend_from_slice(&ciphertext);
    let signature = hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, &key.signing_key), &token);
    token.extend_from_slice(signature.as_ref());
    Ok(URL_SAFE.encode(token))
}

pub fn fernet_decrypt(fernet_key: &str, token: &str) -> Result<Vec<u8>, A2APushSecretError> {
    let key = FernetKey::decode(fernet_key)?;
    let token = decode_base64_url(token)?;
    let minimum_len = 1 + 8 + FERNET_IV_LEN + AES_BLOCK_LEN + FERNET_HMAC_LEN;
    if token.len() < minimum_len || token[0] != FERNET_VERSION {
        return Err(decrypt_error());
    }
    let signed_len = token.len() - FERNET_HMAC_LEN;
    hmac::verify(
        &hmac::Key::new(hmac::HMAC_SHA256, &key.signing_key),
        &token[..signed_len],
        &token[signed_len..],
    )
    .map_err(|_| decrypt_error())?;

    let iv: [u8; FERNET_IV_LEN] = token[9..25].try_into().map_err(|_| decrypt_error())?;
    aes_128_cbc_decrypt(&key.encryption_key, &iv, &token[25..signed_len])
        .map_err(|_| decrypt_error())
}

pub(in crate::push_secrets) fn fernet_encrypt(
    fernet_key: &str,
    plaintext: &[u8],
) -> Result<String, A2APushSecretError> {
    let mut iv = [0_u8; FERNET_IV_LEN];
    rand::SystemRandom::new()
        .fill(&mut iv)
        .map_err(|_| A2APushSecretError::new("A2A push secret random generation failed"))?;
    fernet_encrypt_at(fernet_key, plaintext, current_time_seconds(), iv)
}

#[derive(Clone, Debug)]
pub(in crate::push_secrets) struct FernetKey {
    signing_key: [u8; FERNET_SIGNING_KEY_LEN],
    encryption_key: [u8; FERNET_AES_KEY_LEN],
}

impl FernetKey {
    pub(in crate::push_secrets) fn decode(value: &str) -> Result<Self, A2APushSecretError> {
        let key = decode_base64_url(value)?;
        if key.len() != FERNET_KEY_LEN {
            return Err(A2APushSecretError::new(
                "A2A push secret keyring is malformed",
            ));
        }
        Ok(Self {
            signing_key: key[..FERNET_SIGNING_KEY_LEN]
                .try_into()
                .map_err(|_| A2APushSecretError::new("A2A push secret keyring is malformed"))?,
            encryption_key: key[FERNET_SIGNING_KEY_LEN..]
                .try_into()
                .map_err(|_| A2APushSecretError::new("A2A push secret keyring is malformed"))?,
        })
    }
}

pub(in crate::push_secrets) fn generate_fernet_key() -> Result<String, A2APushSecretError> {
    let mut key = [0_u8; FERNET_KEY_LEN];
    rand::SystemRandom::new()
        .fill(&mut key)
        .map_err(|_| A2APushSecretError::new("A2A push secret random generation failed"))?;
    Ok(URL_SAFE.encode(key))
}

fn decode_base64_url(value: &str) -> Result<Vec<u8>, A2APushSecretError> {
    let normalized = normalize_base64_padding(value);
    URL_SAFE
        .decode(normalized.as_bytes())
        .map_err(|_| A2APushSecretError::new("A2A push secret ciphertext could not be decrypted"))
}

fn normalize_base64_padding(value: &str) -> String {
    let mut value = value.to_owned();
    let remainder = value.len() % 4;
    if remainder > 0 {
        value.extend(std::iter::repeat_n('=', 4 - remainder));
    }
    value
}

fn decrypt_error() -> A2APushSecretError {
    A2APushSecretError::new("A2A push secret ciphertext could not be decrypted")
}
