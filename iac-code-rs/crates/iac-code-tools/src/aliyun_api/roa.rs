use std::collections::BTreeMap;

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json::JsonValue;

use super::encoding::{form_encode, hmac_sha256_hex, sha256_hex};
use super::time::{current_timestamp, nonce};
use super::{AliyunApiTool, ALIYUN_ROA_USER_AGENT};

#[derive(Clone, Debug)]
pub(super) struct RoaRequest {
    pub(super) action: String,
    pub(super) version: String,
    pub(super) params: BTreeMap<String, String>,
    pub(super) method: String,
    pub(super) pathname: String,
    pub(super) body: Option<JsonValue>,
}

impl AliyunApiTool {
    pub(super) fn call_roa(
        &self,
        endpoint: &str,
        request: RoaRequest,
        credential: &AliyunCredential,
    ) -> Result<String, String> {
        let method = request.method.trim().to_ascii_uppercase();
        let method = if method.is_empty() {
            "POST".to_owned()
        } else {
            method
        };
        let pathname = normalize_roa_pathname(&request.pathname);
        let query = form_encode(&request.params);
        let url = join_endpoint_path_query(endpoint, &pathname, &query);
        let body = request
            .body
            .map(|value| value.to_compact_json())
            .unwrap_or_default();
        let content_sha256 = sha256_hex(body.as_bytes());
        let date = current_timestamp();
        let nonce = nonce();
        let host = host_for_url(&url);

        let mut signed_headers = BTreeMap::from([
            ("accept".to_owned(), "application/json".to_owned()),
            (
                "content-type".to_owned(),
                "application/json; charset=utf-8".to_owned(),
            ),
            ("host".to_owned(), host),
            ("user-agent".to_owned(), ALIYUN_ROA_USER_AGENT.to_owned()),
            ("x-acs-action".to_owned(), request.action.clone()),
            ("x-acs-content-sha256".to_owned(), content_sha256.clone()),
            (
                "x-acs-credentials-provider".to_owned(),
                "static_ak".to_owned(),
            ),
            ("x-acs-date".to_owned(), date.clone()),
            ("x-acs-signature-nonce".to_owned(), nonce.clone()),
            ("x-acs-version".to_owned(), request.version.clone()),
        ]);
        if matches!(credential.mode.as_str(), "StsToken" | "OAuth")
            && !credential.sts_token.is_empty()
        {
            signed_headers.insert("x-acs-security-token".into(), credential.sts_token.clone());
        }
        let signed_header_names = signed_headers.keys().cloned().collect::<Vec<_>>().join(";");
        let canonical_headers = signed_headers
            .iter()
            .map(|(key, value)| format!("{key}:{value}\n"))
            .collect::<String>();
        let canonical_request = format!(
            "{method}\n{pathname}\n{query}\n{canonical_headers}\n{signed_header_names}\n{content_sha256}"
        );
        let string_to_sign = format!(
            "ACS3-HMAC-SHA256\n{}",
            sha256_hex(canonical_request.as_bytes())
        );
        let signature = hmac_sha256_hex(&credential.access_key_secret, &string_to_sign);
        let authorization = format!(
            "ACS3-HMAC-SHA256 Credential={},SignedHeaders={},Signature={}",
            credential.access_key_id, signed_header_names, signature
        );
        let method = reqwest::Method::from_bytes(method.as_bytes())
            .map_err(|error| format!("Invalid ROA method '{method}': {error}"))?;
        let mut request = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|error| format!("Failed to call Aliyun API: {error}"))?
            .request(method, url)
            .header(reqwest::header::ACCEPT, "application/json")
            .header(
                reqwest::header::CONTENT_TYPE,
                "application/json; charset=utf-8",
            )
            .header(reqwest::header::USER_AGENT, ALIYUN_ROA_USER_AGENT)
            .header("x-acs-version", request.version)
            .header("x-acs-action", request.action)
            .header("x-acs-date", date)
            .header("x-acs-signature-nonce", nonce)
            .header("x-acs-content-sha256", content_sha256)
            .header("x-acs-credentials-provider", "static_ak")
            .header(reqwest::header::AUTHORIZATION, authorization)
            .body(body);
        if matches!(credential.mode.as_str(), "StsToken" | "OAuth")
            && !credential.sts_token.is_empty()
        {
            request = request.header("x-acs-security-token", &credential.sts_token);
        }

        let response = request
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

fn normalize_roa_pathname(pathname: &str) -> String {
    let pathname = pathname.trim();
    if pathname.is_empty() {
        "/".into()
    } else if pathname.starts_with('/') {
        pathname.to_owned()
    } else {
        format!("/{pathname}")
    }
}

fn join_endpoint_path_query(endpoint: &str, pathname: &str, query: &str) -> String {
    let mut url = format!("{}{}", endpoint.trim_end_matches('/'), pathname);
    if !query.is_empty() {
        url.push('?');
        url.push_str(query);
    }
    url
}

fn host_for_url(url: &str) -> String {
    let Ok(url) = reqwest::Url::parse(url) else {
        return String::new();
    };
    let Some(host) = url.host_str() else {
        return String::new();
    };
    match url.port() {
        Some(port) => format!("{host}:{port}"),
        None => host.to_owned(),
    }
}
