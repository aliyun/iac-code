use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_a2a::push_secrets::{
    fernet_decrypt, fernet_encrypt_at, A2APushSecretEnvelope, A2APushSecretKeyring,
};

const PYTHON_FERNET_KEY: &str = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=";
const PYTHON_FERNET_TOKEN: &str =
    "gAAAAABlU_EAEBESExQVFhcYGRobHB0eH-PL8hlOsFk83vaJHIwd73emw-xQHoM-bLNpYv_5oKQU2zutDFYIUMNJZVhc2tZN-w==";

#[test]
fn fernet_decrypts_python_known_token() {
    let plaintext = fernet_decrypt(PYTHON_FERNET_KEY, PYTHON_FERNET_TOKEN).unwrap();

    assert_eq!(String::from_utf8(plaintext).unwrap(), "hello fernet");
}

#[test]
fn fernet_encrypts_python_known_token_with_fixed_iv() {
    let iv = [
        16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    ];

    let token = fernet_encrypt_at(PYTHON_FERNET_KEY, b"hello fernet", 1_700_000_000, iv).unwrap();

    assert_eq!(token, PYTHON_FERNET_TOKEN);
}

#[test]
fn local_keyring_round_trips_persists_and_rotates() {
    let _guard = env_guard();
    let root = temp_dir("local-keyring");
    let path = root.join("push_keys.json");
    std::env::remove_var("IAC_CODE_A2A_PUSH_KEYRING");

    let mut keyring = A2APushSecretKeyring::new(&path);
    let envelope = keyring.encrypt("shared secret").unwrap();
    let old_key_id = envelope.key_id().to_owned();

    assert!(old_key_id.starts_with("push-"));
    assert!(path.exists());
    assert_eq!(keyring.decrypt(&envelope).unwrap(), "shared secret");

    let new_key_id = keyring.rotate(None).unwrap();
    assert_ne!(new_key_id, old_key_id);
    assert_eq!(keyring.decrypt(&envelope).unwrap(), "shared secret");

    let new_envelope = keyring.encrypt("new secret").unwrap();
    assert_eq!(new_envelope.key_id(), new_key_id);

    let mut reloaded = A2APushSecretKeyring::new(&path);
    assert_eq!(reloaded.decrypt(&envelope).unwrap(), "shared secret");
    assert_eq!(reloaded.decrypt(&new_envelope).unwrap(), "new secret");

    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn keyring_uses_environment_managed_keys() {
    let _guard = env_guard();
    let root = temp_dir("env-keyring");
    let producer_path = root.join("producer.json");
    let consumer_path = root.join("consumer.json");
    std::env::set_var(
        "IAC_CODE_A2A_PUSH_KEYRING",
        format!(
            r#"{{"activeKeyId":"shared","keys":[{{"id":"shared","fernetKey":"{PYTHON_FERNET_KEY}"}}]}}"#
        ),
    );

    let mut producer = A2APushSecretKeyring::new(&producer_path);
    let mut consumer = A2APushSecretKeyring::new(&consumer_path);
    let envelope = producer.encrypt("shared secret").unwrap();

    assert_eq!(consumer.decrypt(&envelope).unwrap(), "shared secret");
    assert_eq!(producer.active_key_id().unwrap(), "shared");
    assert!(!producer_path.exists());
    assert!(producer
        .rotate(None)
        .unwrap_err()
        .to_string()
        .contains("environment-managed"));

    std::env::remove_var("IAC_CODE_A2A_PUSH_KEYRING");
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn keyring_rejects_missing_key_and_invalid_token() {
    let _guard = env_guard();
    let root = temp_dir("bad-token");
    let path = root.join("push_keys.json");
    std::env::remove_var("IAC_CODE_A2A_PUSH_KEYRING");

    let mut keyring = A2APushSecretKeyring::new(&path);
    let missing_key = A2APushSecretEnvelope::new("missing", PYTHON_FERNET_TOKEN);
    assert!(keyring
        .decrypt(&missing_key)
        .unwrap_err()
        .to_string()
        .contains("not available"));

    let envelope = keyring.encrypt("secret").unwrap();
    let tampered = A2APushSecretEnvelope::new(envelope.key_id(), "not-a-fernet-token");
    assert!(keyring
        .decrypt(&tampered)
        .unwrap_err()
        .to_string()
        .contains("could not be decrypted"));

    let _ = std::fs::remove_dir_all(root);
}

fn env_guard() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
}

fn temp_dir(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!(
        "iac-code-a2a-{name}-{}-{nonce}",
        std::process::id()
    ))
}
