use std::time::Duration;

use iac_code_protocol::json::JsonValue;

use crate::push_endpoint::PinnedCallbackRequest;

use super::A2APushDeliveryError;

pub(super) fn build_callback_client(
    pinned: &PinnedCallbackRequest,
) -> Result<reqwest::blocking::Client, A2APushDeliveryError> {
    reqwest::blocking::Client::builder()
        .pool_max_idle_per_host(0)
        .redirect(reqwest::redirect::Policy::none())
        .resolve_to_addrs(&pinned.sni_hostname, &pinned.resolved_addresses)
        .build()
        .map_err(|error| A2APushDeliveryError::transport(error.to_string()))
}

pub(super) fn build_json_request(
    client: &reqwest::blocking::Client,
    pinned: PinnedCallbackRequest,
    payload: &JsonValue,
    timeout_seconds: f64,
) -> reqwest::blocking::RequestBuilder {
    let mut request = client
        .post(&pinned.url)
        .timeout(Duration::from_secs_f64(timeout_seconds.max(0.0)))
        .header(reqwest::header::CONTENT_TYPE, "application/json");
    for (key, value) in pinned.headers {
        request = request.header(key, value);
    }
    request.body(payload.to_compact_json())
}

pub(super) fn classify_send_error(error: reqwest::Error) -> A2APushDeliveryError {
    if error.is_timeout() {
        A2APushDeliveryError::timeout(error.to_string())
    } else {
        A2APushDeliveryError::transport(error.to_string())
    }
}
