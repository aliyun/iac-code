use std::io;
use std::os::fd::RawFd;

use iac_code_config::cloud_credentials::{
    save_aliyun_credentials, AliyunCredential, DEFAULT_REGION,
};
use iac_code_config::paths::ConfigPaths;
use iac_code_tui::RawInputCapture;

use crate::cli_i18n::tr;
use crate::raw_auth::read_raw_auth_index_picker;
use crate::raw_auth_oauth::{raw_auth_aliyun_oauth_sites, run_raw_auth_aliyun_oauth_login};

pub(super) fn read_raw_auth_aliyun_oauth_login_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    existing_credential: Option<&AliyunCredential>,
) -> io::Result<Option<String>> {
    let sites = raw_auth_aliyun_oauth_sites();
    let site_options = sites
        .iter()
        .map(|site| tr(site.display_name))
        .collect::<Vec<_>>();
    let default_site_index = existing_credential
        .and_then(|credential| {
            sites
                .iter()
                .position(|site| site.site_type == credential.oauth_site_type)
        })
        .unwrap_or(0);
    let Some(site_index) = read_raw_auth_index_picker(
        fd,
        capture,
        &tr("Choose site type"),
        &site_options,
        default_site_index,
    )?
    else {
        return Ok(None);
    };
    let site = &sites[site_index];

    let oauth_result = match run_raw_auth_aliyun_oauth_login(fd, capture, site) {
        Ok(result) => result,
        Err(error) if error == tr("OAuth login cancelled.") => return Ok(None),
        Err(error) => {
            return Ok(Some(
                tr("Alibaba Cloud OAuth login failed: {error}").replace("{error}", &error),
            ));
        }
    };

    let credential = AliyunCredential {
        mode: "OAuth".to_owned(),
        region_id: existing_credential
            .map(|credential| credential.region_id.clone())
            .filter(|region| !region.is_empty())
            .unwrap_or_else(|| DEFAULT_REGION.to_owned()),
        oauth_site_type: site.site_type.to_owned(),
        oauth_access_token: oauth_result.token.access_token,
        oauth_refresh_token: oauth_result.token.refresh_token,
        oauth_access_token_expire: oauth_result.token.access_token_expire,
        oauth_refresh_token_expire: oauth_result.token.refresh_token_expire,
        access_key_id: oauth_result.sts.access_key_id,
        access_key_secret: oauth_result.sts.access_key_secret,
        sts_token: oauth_result.sts.sts_token,
        sts_expiration: oauth_result.sts.sts_expiration,
        ..AliyunCredential::default()
    };
    save_aliyun_credentials(&paths.cloud_credentials_path, &credential)
        .map_err(|error| io::Error::other(error.to_string()))?;
    Ok(Some(tr(
        "Configured: Alibaba Cloud OAuth credentials saved",
    )))
}
