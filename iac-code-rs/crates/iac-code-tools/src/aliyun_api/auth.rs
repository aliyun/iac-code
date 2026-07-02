use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_config::cloud_credentials::{save_aliyun_credentials, AliyunCredential};

use super::oauth::{
    exchange_oauth_access_token_for_sts, is_epoch_expired, oauth_relogin_message,
    refresh_oauth_access_token, OAuthSite,
};
use super::{AliyunApiTool, ACCESS_TOKEN_SKEW_SECONDS, STS_SKEW_SECONDS};

impl AliyunApiTool {
    pub(super) fn credential_for_call(&self) -> Result<AliyunCredential, String> {
        let Some(mut credential) = self.credential.clone() else {
            return Err(
                "Alibaba Cloud credentials not configured. Run 'iac-code auth' and select 'Cloud Provider' to configure."
                    .into(),
            );
        };
        self.refresh_oauth_if_needed(&mut credential)?;
        Ok(credential)
    }

    fn refresh_oauth_if_needed(&self, credential: &mut AliyunCredential) -> Result<(), String> {
        if credential.mode != "OAuth" {
            return Ok(());
        }

        let now = self.current_epoch_seconds();
        let has_sts = !credential.access_key_id.is_empty()
            && !credential.access_key_secret.is_empty()
            && !credential.sts_token.is_empty();
        if has_sts && !is_epoch_expired(credential.sts_expiration, now, STS_SKEW_SECONDS) {
            return Ok(());
        }

        if credential.oauth_site_type.is_empty() {
            return Err(oauth_relogin_message(
                "Alibaba Cloud OAuth site is missing.",
            ));
        }

        let site = OAuthSite::resolve(
            &credential.oauth_site_type,
            self.oauth_base_url_override.as_deref(),
        )?;
        if is_epoch_expired(
            credential.oauth_access_token_expire,
            now,
            ACCESS_TOKEN_SKEW_SECONDS,
        ) {
            if credential.oauth_refresh_token.is_empty() {
                return Err(oauth_relogin_message(
                    "Alibaba Cloud OAuth refresh token is missing.",
                ));
            }
            let token = refresh_oauth_access_token(&site, &credential.oauth_refresh_token, now)?;
            credential.oauth_access_token = token.access_token;
            credential.oauth_refresh_token = token.refresh_token;
            credential.oauth_access_token_expire = token.access_token_expire;
            credential.oauth_refresh_token_expire = token.refresh_token_expire;
        }

        if credential.oauth_access_token.is_empty() {
            return Err(oauth_relogin_message(
                "Alibaba Cloud OAuth access token is missing.",
            ));
        }

        let sts = exchange_oauth_access_token_for_sts(&site, &credential.oauth_access_token)?;
        credential.access_key_id = sts.access_key_id;
        credential.access_key_secret = sts.access_key_secret;
        credential.sts_token = sts.sts_token;
        credential.sts_expiration = sts.sts_expiration;

        if let Some(path) = &self.cloud_credentials_path {
            save_aliyun_credentials(path, credential)
                .map_err(|error| format!("Failed to save Alibaba Cloud credentials: {error}"))?;
        }

        Ok(())
    }

    fn current_epoch_seconds(&self) -> i64 {
        self.now_epoch_seconds.unwrap_or_else(|| {
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.as_secs() as i64)
                .unwrap_or_default()
        })
    }
}
