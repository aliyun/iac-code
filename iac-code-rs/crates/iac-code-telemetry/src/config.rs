use std::env;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrivacyLevel {
    Default,
    NoTelemetry,
    EssentialTraffic,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContentCaptureMode {
    NoContent,
    SpanOnly,
    EventOnly,
    SpanAndEvent,
}

pub fn get_privacy_level() -> PrivacyLevel {
    if is_env_truthy("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC") {
        return PrivacyLevel::EssentialTraffic;
    }
    if is_env_truthy("DISABLE_TELEMETRY") {
        return PrivacyLevel::NoTelemetry;
    }
    PrivacyLevel::Default
}

pub fn is_telemetry_disabled_for_release_date(release_date: &str) -> bool {
    release_date.trim().is_empty() || get_privacy_level() != PrivacyLevel::Default
}

pub fn is_telemetry_disabled() -> bool {
    is_telemetry_disabled_for_release_date(option_env!("IAC_CODE_RELEASE_DATE").unwrap_or(""))
}

pub fn is_essential_traffic_only() -> bool {
    get_privacy_level() == PrivacyLevel::EssentialTraffic
}

pub fn get_content_capture_mode() -> ContentCaptureMode {
    match env::var("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
        .unwrap_or_default()
        .trim()
        .to_ascii_uppercase()
        .as_str()
    {
        "SPAN_ONLY" => ContentCaptureMode::SpanOnly,
        "EVENT_ONLY" => ContentCaptureMode::EventOnly,
        "SPAN_AND_EVENT" => ContentCaptureMode::SpanAndEvent,
        _ => ContentCaptureMode::NoContent,
    }
}

pub fn should_capture_content_on_span(debug_enabled: bool) -> bool {
    if debug_enabled {
        return true;
    }
    matches!(
        get_content_capture_mode(),
        ContentCaptureMode::SpanOnly | ContentCaptureMode::SpanAndEvent
    )
}

fn is_env_truthy(name: &str) -> bool {
    matches!(
        env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}
