use iac_code_config::cloud_credentials::AliyunCredential;

use crate::cli_i18n::tr;

pub(super) const RAW_AUTH_ALIYUN_CREDENTIAL_MODES: &[&str] =
    &["AK", "StsToken", "RamRoleArn", "OAuth"];

#[derive(Clone, Copy, Debug)]
struct RawAuthAliyunCredentialField {
    name: &'static str,
    label: &'static str,
    sensitive: bool,
}

static RAW_AUTH_ALIYUN_AK_FIELDS: &[RawAuthAliyunCredentialField] = &[
    RawAuthAliyunCredentialField {
        name: "access_key_id",
        label: "AccessKey ID",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "access_key_secret",
        label: "AccessKey Secret",
        sensitive: true,
    },
];

static RAW_AUTH_ALIYUN_STS_FIELDS: &[RawAuthAliyunCredentialField] = &[
    RawAuthAliyunCredentialField {
        name: "access_key_id",
        label: "AccessKey ID",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "access_key_secret",
        label: "AccessKey Secret",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "sts_token",
        label: "STS Token",
        sensitive: true,
    },
];

static RAW_AUTH_ALIYUN_RAM_ROLE_FIELDS: &[RawAuthAliyunCredentialField] = &[
    RawAuthAliyunCredentialField {
        name: "access_key_id",
        label: "AccessKey ID",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "access_key_secret",
        label: "AccessKey Secret",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "ram_role_arn",
        label: "RAM Role ARN",
        sensitive: false,
    },
    RawAuthAliyunCredentialField {
        name: "ram_session_name",
        label: "Session Name",
        sensitive: false,
    },
];

static RAW_AUTH_ALIYUN_OAUTH_FIELDS: &[RawAuthAliyunCredentialField] = &[
    RawAuthAliyunCredentialField {
        name: "oauth_site_type",
        label: "OAuth Site Type",
        sensitive: false,
    },
    RawAuthAliyunCredentialField {
        name: "oauth_access_token",
        label: "OAuth Access Token",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "oauth_refresh_token",
        label: "OAuth Refresh Token",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "oauth_access_token_expire",
        label: "OAuth Access Token Expire",
        sensitive: false,
    },
    RawAuthAliyunCredentialField {
        name: "oauth_refresh_token_expire",
        label: "OAuth Refresh Token Expire",
        sensitive: false,
    },
    RawAuthAliyunCredentialField {
        name: "access_key_id",
        label: "AccessKey ID",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "access_key_secret",
        label: "AccessKey Secret",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "sts_token",
        label: "STS Token",
        sensitive: true,
    },
    RawAuthAliyunCredentialField {
        name: "sts_expiration",
        label: "STS Expiration",
        sensitive: false,
    },
];

pub(super) fn raw_auth_aliyun_credential_mode_label(mode: &str) -> String {
    match mode {
        "AK" => tr("AccessKey"),
        "StsToken" => tr("STS Token"),
        "RamRoleArn" => tr("RAM Role"),
        "OAuth" => tr("OAuth Login (Browser)"),
        _ => mode.to_owned(),
    }
}

pub(super) fn raw_auth_aliyun_credential_info_lines(
    credential: &AliyunCredential,
    source: &str,
) -> Vec<String> {
    let mut lines = vec![
        format!("{} ({source})", tr("Current configuration")),
        format!(
            "{}: {}",
            tr("Mode"),
            raw_auth_aliyun_credential_mode_label(&credential.mode)
        ),
    ];
    for field in raw_auth_aliyun_credential_fields(&credential.mode) {
        let value = raw_auth_aliyun_credential_field_display_value(credential, field);
        let display_value = if value.is_empty() {
            tr("(not set)")
        } else {
            value
        };
        lines.push(format!("{}: {display_value}", tr(field.label)));
    }
    lines.push(format!("{}: {}", tr("Region"), credential.region_id));
    lines
}

fn raw_auth_aliyun_credential_fields(mode: &str) -> &'static [RawAuthAliyunCredentialField] {
    match mode {
        "AK" => RAW_AUTH_ALIYUN_AK_FIELDS,
        "StsToken" => RAW_AUTH_ALIYUN_STS_FIELDS,
        "RamRoleArn" => RAW_AUTH_ALIYUN_RAM_ROLE_FIELDS,
        "OAuth" => RAW_AUTH_ALIYUN_OAUTH_FIELDS,
        _ => &[],
    }
}

fn raw_auth_aliyun_credential_field_display_value(
    credential: &AliyunCredential,
    field: &RawAuthAliyunCredentialField,
) -> String {
    let value = match field.name {
        "access_key_id" => credential.access_key_id.clone(),
        "access_key_secret" => credential.access_key_secret.clone(),
        "sts_token" => credential.sts_token.clone(),
        "sts_expiration" => raw_auth_format_epoch(credential.sts_expiration),
        "ram_role_arn" => credential.ram_role_arn.clone(),
        "ram_session_name" => credential.ram_session_name.clone(),
        "oauth_site_type" => credential.oauth_site_type.clone(),
        "oauth_access_token" => credential.oauth_access_token.clone(),
        "oauth_refresh_token" => credential.oauth_refresh_token.clone(),
        "oauth_access_token_expire" => raw_auth_format_epoch(credential.oauth_access_token_expire),
        "oauth_refresh_token_expire" => {
            raw_auth_format_epoch(credential.oauth_refresh_token_expire)
        }
        _ => String::new(),
    };
    if field.sensitive && !value.is_empty() {
        "*".repeat(value.chars().count())
    } else {
        value
    }
}

fn raw_auth_format_epoch(epoch: i64) -> String {
    if epoch <= 0 {
        return String::new();
    }
    let days = epoch.div_euclid(86_400);
    let seconds_of_day = epoch.rem_euclid(86_400);
    let (year, month, day) = raw_auth_civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02} (UTC)")
}

fn raw_auth_civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    let year = year + i64::from(month <= 2);
    (year, month, day)
}
