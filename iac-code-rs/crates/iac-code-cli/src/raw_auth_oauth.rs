use std::os::fd::RawFd;

use iac_code_tui::RawInputCapture;

use crate::raw_auth_oauth_browser::{
    raw_auth_aliyun_oauth_authorization_url, raw_auth_open_browser, raw_auth_pkce_challenge,
    raw_auth_random_url_token,
};
use crate::raw_auth_oauth_callback::RawAuthAliyunOAuthCallback;
use crate::raw_auth_oauth_client::{
    raw_auth_aliyun_oauth_exchange_access_token_for_sts,
    raw_auth_aliyun_oauth_exchange_code_for_token,
};
use crate::raw_auth_oauth_fake::raw_auth_fake_aliyun_oauth_result;
use crate::raw_auth_oauth_render::render_raw_auth_aliyun_oauth_waiting;
use crate::raw_auth_oauth_types::{RawAuthAliyunOAuthResult, RawAuthAliyunOAuthSite};

pub(super) use crate::raw_auth_oauth_types::raw_auth_aliyun_oauth_sites;

pub(super) fn run_raw_auth_aliyun_oauth_login(
    fd: RawFd,
    capture: &RawInputCapture,
    site: &RawAuthAliyunOAuthSite,
) -> Result<RawAuthAliyunOAuthResult, String> {
    if let Some(result) = raw_auth_fake_aliyun_oauth_result()? {
        return Ok(result);
    }

    let state = raw_auth_random_url_token(24);
    let code_verifier = raw_auth_random_url_token(96);
    let code_challenge = raw_auth_pkce_challenge(&code_verifier);
    let callback = RawAuthAliyunOAuthCallback::start()?;
    let authorization_url = raw_auth_aliyun_oauth_authorization_url(
        site,
        &callback.redirect_uri,
        &state,
        &code_challenge,
    );
    render_raw_auth_aliyun_oauth_waiting(fd, &authorization_url)
        .map_err(|error| error.to_string())?;
    raw_auth_open_browser(&authorization_url);

    let code = callback.wait_for_code(capture, &state)?;
    let token = raw_auth_aliyun_oauth_exchange_code_for_token(
        site,
        &code,
        &callback.redirect_uri,
        &code_verifier,
    )?;
    let sts = raw_auth_aliyun_oauth_exchange_access_token_for_sts(site, &token.access_token)?;
    Ok(RawAuthAliyunOAuthResult { token, sts })
}
