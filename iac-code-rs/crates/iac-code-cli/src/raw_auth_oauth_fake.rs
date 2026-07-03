use std::collections::BTreeMap;
use std::env;

use crate::raw_auth_oauth_types::{
    RawAuthAliyunOAuthResult, RawAuthAliyunOAuthSts, RawAuthAliyunOAuthToken,
};

pub(super) fn raw_auth_fake_aliyun_oauth_result() -> Result<Option<RawAuthAliyunOAuthResult>, String>
{
    let Ok(raw) = env::var("IAC_CODE_RS_FAKE_ALIYUN_OAUTH_RESULT") else {
        return Ok(None);
    };
    let mut values = BTreeMap::new();
    for line in raw.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        values.insert(key.trim().to_owned(), value.trim().to_owned());
    }
    let string = |key: &str| {
        values
            .get(key)
            .filter(|value| !value.is_empty())
            .cloned()
            .ok_or_else(|| format!("fake OAuth result missing {key}"))
    };
    let integer = |key: &str| {
        string(key)?
            .parse::<i64>()
            .map_err(|_| format!("fake OAuth result has invalid {key}"))
    };
    Ok(Some(RawAuthAliyunOAuthResult {
        token: RawAuthAliyunOAuthToken {
            access_token: string("oauth_access_token")?,
            refresh_token: string("oauth_refresh_token")?,
            access_token_expire: integer("oauth_access_token_expire")?,
            refresh_token_expire: values
                .get("oauth_refresh_token_expire")
                .filter(|value| !value.is_empty())
                .map(|value| {
                    value.parse::<i64>().map_err(|_| {
                        "fake OAuth result has invalid oauth_refresh_token_expire".to_owned()
                    })
                })
                .transpose()?
                .unwrap_or_default(),
        },
        sts: RawAuthAliyunOAuthSts {
            access_key_id: string("access_key_id")?,
            access_key_secret: string("access_key_secret")?,
            sts_token: string("sts_token")?,
            sts_expiration: integer("sts_expiration")?,
        },
    }))
}
