use std::time::Duration;

pub fn probe_openapi_compatible(
    base_url: &str,
    api_key: Option<&str>,
    model: &str,
    timeout: Duration,
) -> Option<bool> {
    let client = reqwest::blocking::Client::builder()
        .timeout(timeout)
        .build()
        .ok()?;
    let url = format!("{}/models", base_url.trim_end_matches('/'));
    let mut request = client.get(url);
    if let Some(api_key) = api_key {
        request = request.bearer_auth(api_key);
    }

    let response = request.send().ok()?;
    if !response.status().is_success() {
        return None;
    }
    let text = response.text().ok()?;
    let payload = serde_json::from_str::<serde_json::Value>(&text).ok()?;
    probe_modalities_from_payload(&payload, model)
}

fn probe_modalities_from_payload(payload: &serde_json::Value, model: &str) -> Option<bool> {
    let data = payload.get("data")?.as_array()?;
    for entry in data {
        if entry.get("id").and_then(serde_json::Value::as_str) != Some(model) {
            continue;
        }
        let modalities = entry
            .get("architecture")
            .and_then(|architecture| architecture.get("input_modalities"))
            .and_then(serde_json::Value::as_array)?;
        return Some(
            modalities
                .iter()
                .any(|modality| modality.as_str() == Some("image")),
        );
    }
    None
}
