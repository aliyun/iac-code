use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ring::digest;
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::process::{self, Command};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::raw_auth_oauth_types::RawAuthAliyunOAuthSite;
use crate::raw_auth_oauth_utils::raw_auth_form_encode;

pub(super) fn raw_auth_aliyun_oauth_authorization_url(
    site: &RawAuthAliyunOAuthSite,
    redirect_uri: &str,
    state: &str,
    code_challenge: &str,
) -> String {
    let mut query = BTreeMap::new();
    query.insert("response_type".to_owned(), "code".to_owned());
    query.insert("client_id".to_owned(), site.client_id.to_owned());
    query.insert("redirect_uri".to_owned(), redirect_uri.to_owned());
    query.insert("state".to_owned(), state.to_owned());
    query.insert("code_challenge".to_owned(), code_challenge.to_owned());
    query.insert("code_challenge_method".to_owned(), "S256".to_owned());
    format!(
        "{}/oauth2/v1/auth?{}",
        site.signin_base_url.trim_end_matches('/'),
        raw_auth_form_encode(&query)
    )
}

pub(super) fn raw_auth_pkce_challenge(code_verifier: &str) -> String {
    URL_SAFE_NO_PAD.encode(digest::digest(&digest::SHA256, code_verifier.as_bytes()).as_ref())
}

pub(super) fn raw_auth_random_url_token(byte_len: usize) -> String {
    let mut bytes = vec![0_u8; byte_len];
    let random_ok = fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .is_ok();
    if !random_ok {
        let mut seed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
            ^ ((process::id() as u128) << 64);
        for byte in &mut bytes {
            *byte = seed as u8;
            seed = seed.rotate_left(7) ^ 0x9e37_79b9_7f4a_7c15_u128;
        }
    }
    URL_SAFE_NO_PAD.encode(bytes)
}

pub(super) fn raw_auth_open_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("open");
        command.arg(url);
        command
    };
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("cmd");
        command.args(["/C", "start", "", url]);
        command
    };
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut command = {
        let mut command = Command::new("xdg-open");
        command.arg(url);
        command
    };
    let _ = command.spawn();
}
