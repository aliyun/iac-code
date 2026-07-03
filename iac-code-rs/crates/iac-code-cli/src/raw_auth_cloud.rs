use std::io;
use std::os::fd::RawFd;

use iac_code_config::cloud_credentials::{
    load_aliyun_credentials_from_iac_code_config, save_aliyun_credentials, AliyunCredential,
    DEFAULT_REGION,
};
use iac_code_config::paths::ConfigPaths;
use iac_code_tui::RawInputCapture;

use crate::cli_i18n::tr;
use crate::raw_auth::{
    raw_auth_label, read_raw_auth_index_picker, read_raw_auth_index_picker_with_info,
};
use crate::raw_auth_cloud_fields::{
    raw_auth_aliyun_credential_info_lines, raw_auth_aliyun_credential_mode_label,
    RAW_AUTH_ALIYUN_CREDENTIAL_MODES,
};
use crate::raw_auth_cloud_oauth::read_raw_auth_aliyun_oauth_login_flow;
use crate::raw_auth_input::{read_raw_auth_masked_input, read_raw_auth_text_input};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum RawAuthCloudProviderChoice {
    AlibabaCloud,
}

pub(super) fn raw_auth_cloud_provider_choice(index: usize) -> Option<RawAuthCloudProviderChoice> {
    match index {
        0 => Some(RawAuthCloudProviderChoice::AlibabaCloud),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum RawAuthAliyunOptionChoice {
    Credential,
    Region,
}

pub(super) fn raw_auth_aliyun_option_choice(index: usize) -> Option<RawAuthAliyunOptionChoice> {
    match index {
        0 => Some(RawAuthAliyunOptionChoice::Credential),
        1 => Some(RawAuthAliyunOptionChoice::Region),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum RawAuthAliyunExistingCredentialAction {
    Reconfigure,
    Back,
}

pub(super) fn raw_auth_aliyun_existing_credential_action(
    index: usize,
) -> Option<RawAuthAliyunExistingCredentialAction> {
    match index {
        0 => Some(RawAuthAliyunExistingCredentialAction::Reconfigure),
        1 => Some(RawAuthAliyunExistingCredentialAction::Back),
        _ => None,
    }
}

pub(super) fn raw_auth_aliyun_credential_mode(index: usize) -> Option<&'static str> {
    RAW_AUTH_ALIYUN_CREDENTIAL_MODES.get(index).copied()
}

#[cfg(unix)]
pub(super) fn read_raw_auth_cloud_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
) -> io::Result<Option<String>> {
    let provider_options = vec![tr("Alibaba Cloud")];
    let Some(provider_index) = read_raw_auth_index_picker(
        fd,
        capture,
        &tr("Select Cloud Provider"),
        &provider_options,
        0,
    )?
    else {
        return Ok(None);
    };
    match raw_auth_cloud_provider_choice(provider_index) {
        Some(RawAuthCloudProviderChoice::AlibabaCloud) => {
            read_raw_auth_aliyun_flow(fd, capture, paths)
        }
        None => Ok(None),
    }
}

#[cfg(unix)]
fn read_raw_auth_aliyun_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
) -> io::Result<Option<String>> {
    let options = vec![tr("Credential"), tr("Region")];
    let Some(option_index) =
        read_raw_auth_index_picker(fd, capture, &tr("Configure Alibaba Cloud"), &options, 0)?
    else {
        return Ok(None);
    };
    match raw_auth_aliyun_option_choice(option_index) {
        Some(RawAuthAliyunOptionChoice::Credential) => {
            read_raw_auth_aliyun_ak_credential_flow(fd, capture, paths)
        }
        Some(RawAuthAliyunOptionChoice::Region) => {
            read_raw_auth_aliyun_region_flow(fd, capture, paths)
        }
        None => Ok(None),
    }
}

#[cfg(unix)]
fn read_raw_auth_aliyun_ak_credential_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
) -> io::Result<Option<String>> {
    let existing = load_raw_auth_aliyun_iac_credential(paths)?;
    loop {
        if let Some(credential) = &existing {
            let info_lines = raw_auth_aliyun_credential_info_lines(credential, "iac-code");
            let action_options = vec![tr("Reconfigure credential"), tr("Back")];
            let Some(action_index) = read_raw_auth_index_picker_with_info(
                fd,
                capture,
                &tr("Configure Alibaba Cloud credentials"),
                &info_lines,
                &action_options,
                0,
            )?
            else {
                return Ok(None);
            };
            match raw_auth_aliyun_existing_credential_action(action_index) {
                Some(RawAuthAliyunExistingCredentialAction::Reconfigure) => {}
                Some(RawAuthAliyunExistingCredentialAction::Back) | None => return Ok(None),
            }
        }

        let credential_modes = RAW_AUTH_ALIYUN_CREDENTIAL_MODES
            .iter()
            .map(|mode| raw_auth_aliyun_credential_mode_label(mode))
            .collect::<Vec<_>>();
        let default_mode_index = existing
            .as_ref()
            .and_then(|credential| {
                RAW_AUTH_ALIYUN_CREDENTIAL_MODES
                    .iter()
                    .position(|mode| *mode == credential.mode)
            })
            .unwrap_or(0);
        let Some(mode_index) = read_raw_auth_index_picker(
            fd,
            capture,
            &tr("Select credential type"),
            &credential_modes,
            default_mode_index,
        )?
        else {
            if existing.is_some() {
                continue;
            }
            return Ok(None);
        };
        let Some(mode) = raw_auth_aliyun_credential_mode(mode_index) else {
            return Ok(None);
        };
        if mode == "OAuth" {
            let result =
                read_raw_auth_aliyun_oauth_login_flow(fd, capture, paths, existing.as_ref())?;
            if result.is_none() && existing.is_some() {
                continue;
            }
            return Ok(result);
        }

        let Some((access_key_id, access_key_secret)) =
            read_raw_auth_aliyun_access_key_fields(fd, capture)?
        else {
            return Ok(None);
        };
        let sts_token = if mode == "StsToken" {
            let Some(sts_token) = read_raw_auth_masked_input(
                fd,
                capture,
                &tr("Configure Alibaba Cloud credentials"),
                &raw_auth_label("STS Token"),
                "",
            )?
            else {
                return Ok(None);
            };
            let sts_token = sts_token.trim();
            if sts_token.is_empty() {
                return Ok(None);
            }
            sts_token.to_owned()
        } else {
            String::new()
        };
        let (ram_role_arn, ram_session_name) = if mode == "RamRoleArn" {
            let Some(ram_role_arn) = read_raw_auth_text_input(
                fd,
                capture,
                &tr("Configure Alibaba Cloud credentials"),
                &raw_auth_label("RAM Role ARN"),
                "",
            )?
            else {
                return Ok(None);
            };
            let ram_role_arn = ram_role_arn.trim();
            if ram_role_arn.is_empty() {
                return Ok(None);
            }

            let Some(ram_session_name) = read_raw_auth_text_input(
                fd,
                capture,
                &tr("Configure Alibaba Cloud credentials"),
                &raw_auth_label("Session Name"),
                "",
            )?
            else {
                return Ok(None);
            };
            let ram_session_name = ram_session_name.trim();
            if ram_session_name.is_empty() {
                return Ok(None);
            }
            (ram_role_arn.to_owned(), ram_session_name.to_owned())
        } else {
            (String::new(), String::new())
        };

        let credential = AliyunCredential {
            mode: mode.to_owned(),
            access_key_id,
            access_key_secret,
            region_id: existing
                .as_ref()
                .map(|credential| credential.region_id.clone())
                .filter(|region| !region.is_empty())
                .unwrap_or_else(|| DEFAULT_REGION.to_owned()),
            sts_token,
            ram_role_arn,
            ram_session_name,
            ..AliyunCredential::default()
        };
        save_aliyun_credentials(&paths.cloud_credentials_path, &credential)
            .map_err(|error| io::Error::other(error.to_string()))?;
        return Ok(Some(tr(
            "Configured: Alibaba Cloud credentials saved to ~/.iac-code",
        )));
    }
}

#[cfg(unix)]
fn read_raw_auth_aliyun_access_key_fields(
    fd: RawFd,
    capture: &RawInputCapture,
) -> io::Result<Option<(String, String)>> {
    let Some(access_key_id) = read_raw_auth_masked_input(
        fd,
        capture,
        &tr("Configure Alibaba Cloud credentials"),
        &raw_auth_label("AccessKey ID"),
        "",
    )?
    else {
        return Ok(None);
    };
    let access_key_id = access_key_id.trim();
    if access_key_id.is_empty() {
        return Ok(None);
    }

    let Some(access_key_secret) = read_raw_auth_masked_input(
        fd,
        capture,
        &tr("Configure Alibaba Cloud credentials"),
        &raw_auth_label("AccessKey Secret"),
        "",
    )?
    else {
        return Ok(None);
    };
    let access_key_secret = access_key_secret.trim();
    if access_key_secret.is_empty() {
        return Ok(None);
    }

    Ok(Some((
        access_key_id.to_owned(),
        access_key_secret.to_owned(),
    )))
}

#[cfg(unix)]
fn read_raw_auth_aliyun_region_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
) -> io::Result<Option<String>> {
    let existing = load_raw_auth_aliyun_iac_credential(paths)?;
    let current_region = existing
        .as_ref()
        .map(|credential| credential.region_id.clone())
        .filter(|region| !region.is_empty())
        .unwrap_or_else(|| DEFAULT_REGION.to_owned());
    let Some(region) = read_raw_auth_text_input(
        fd,
        capture,
        &tr("Configure Alibaba Cloud region"),
        &raw_auth_label("Region"),
        &current_region,
    )?
    else {
        return Ok(None);
    };
    let region = region.trim();
    let region = if region.is_empty() {
        current_region.as_str()
    } else {
        region
    };

    let mut credential = existing.unwrap_or_default();
    credential.region_id = region.to_owned();
    save_aliyun_credentials(&paths.cloud_credentials_path, &credential)
        .map_err(|error| io::Error::other(error.to_string()))?;
    Ok(Some(tr(
        "Configured: Alibaba Cloud region saved to ~/.iac-code",
    )))
}

#[cfg(unix)]
fn load_raw_auth_aliyun_iac_credential(
    paths: &ConfigPaths,
) -> io::Result<Option<AliyunCredential>> {
    load_aliyun_credentials_from_iac_code_config(&paths.cloud_credentials_path)
        .map_err(|error| io::Error::other(error.to_string()))
}
