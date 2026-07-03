use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_config::cloud_credentials::AliyunCredential;
use ring::hmac;

use super::encoding::{aliyun_encode, form_encode};
use super::time::{current_timestamp, nonce};
use super::AliyunApiTool;

impl AliyunApiTool {
    pub(super) fn call_rpc(
        &self,
        endpoint: &str,
        _product: &str,
        action: &str,
        version: &str,
        params: BTreeMap<String, String>,
        credential: &AliyunCredential,
    ) -> Result<String, String> {
        let mut query = params;
        query.insert("Action".into(), action.into());
        query.insert("Version".into(), version.into());
        query.insert("Format".into(), "JSON".into());
        query.insert("AccessKeyId".into(), credential.access_key_id.clone());
        query.insert("SignatureMethod".into(), "HMAC-SHA1".into());
        query.insert("SignatureVersion".into(), "1.0".into());
        query.insert("SignatureNonce".into(), nonce());
        query.insert("Timestamp".into(), current_timestamp());
        if matches!(credential.mode.as_str(), "StsToken" | "OAuth")
            && !credential.sts_token.is_empty()
        {
            query.insert("SecurityToken".into(), credential.sts_token.clone());
        }

        let signature = sign_rpc_query(&query, &credential.access_key_secret);
        query.insert("Signature".into(), signature);
        let body = form_encode(&query);

        let response = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|error| format!("Failed to call Aliyun API: {error}"))?
            .post(endpoint)
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/x-www-form-urlencoded",
            )
            .body(body)
            .send()
            .map_err(|error| format!("Failed to call Aliyun API: {error}"))?;

        let status = response.status();
        let text = response
            .text()
            .map_err(|error| format!("Failed to call Aliyun API: {error}"))?;
        if !status.is_success() {
            return Err(format!("HTTP error {} Response: {text}", status.as_u16()));
        }
        Ok(text)
    }
}

fn sign_rpc_query(query: &BTreeMap<String, String>, access_key_secret: &str) -> String {
    let canonicalized = query
        .iter()
        .map(|(key, value)| format!("{}={}", aliyun_encode(key), aliyun_encode(value)))
        .collect::<Vec<_>>()
        .join("&");
    let string_to_sign = format!("POST&%2F&{}", aliyun_encode(&canonicalized));
    let key = format!("{access_key_secret}&");
    let signature = hmac::sign(
        &hmac::Key::new(hmac::HMAC_SHA1_FOR_LEGACY_USE_ONLY, key.as_bytes()),
        string_to_sign.as_bytes(),
    );
    STANDARD.encode(signature.as_ref())
}
