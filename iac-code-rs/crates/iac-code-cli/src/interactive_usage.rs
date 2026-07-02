use std::time::Duration;

use iac_code_protocol::Usage;

use crate::cli_i18n::tr;

pub(super) fn interactive_completion_status(has_content: bool, elapsed: Duration) -> String {
    if !has_content {
        return String::new();
    }
    if elapsed > Duration::ZERO {
        format!("✱ {} {:.1}s", tr("Processed"), elapsed.as_secs_f64())
    } else {
        format!("✱ {}", tr("Processed"))
    }
}

pub(super) fn interactive_usage_parts(usage: &Usage) -> Vec<String> {
    let mut parts = Vec::new();
    if usage.input_tokens > 0 {
        parts.push(format!(
            "{} {}",
            format_u64_with_compact_suffix(usage.input_tokens),
            interactive_usage_label("Input", "input")
        ));
    }
    if usage.output_tokens > 0 {
        parts.push(format!(
            "{} {}",
            format_u64_with_compact_suffix(usage.output_tokens),
            interactive_usage_label("Output", "output")
        ));
    }
    if usage.cache_creation_input_tokens > 0 {
        parts.push(format!(
            "{} {}",
            format_u64_with_compact_suffix(usage.cache_creation_input_tokens),
            interactive_usage_label("Cache creation", "cache_creation")
        ));
    }
    if usage.cache_read_input_tokens > 0 {
        parts.push(format!(
            "{} {}",
            format_u64_with_compact_suffix(usage.cache_read_input_tokens),
            interactive_usage_label("Cache read", "cache_read")
        ));
    }
    parts
}

fn interactive_usage_label(message: &'static str, english_fallback: &str) -> String {
    let translated = tr(message);
    if translated == message {
        english_fallback.to_owned()
    } else {
        translated
    }
}

pub(super) fn format_u64_with_compact_suffix(value: u64) -> String {
    if value < 1000 {
        return value.to_string();
    }
    let whole = value / 1000;
    let tenths = (value % 1000) / 100;
    if tenths == 0 {
        format!("{whole}k")
    } else {
        format!("{whole}.{tenths}k")
    }
}
