use iac_code_crypto::{fernet_decrypt, fernet_encrypt_at};

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
