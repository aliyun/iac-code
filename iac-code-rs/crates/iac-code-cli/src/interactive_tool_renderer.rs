use std::collections::BTreeMap;

use iac_code_protocol::{json, json::JsonValue};

use crate::ansi::{ANSI_BOLD, ANSI_GREEN, ANSI_RESET};
use crate::cli_i18n::tr;
use crate::json_utils::{json_object_field, json_string_field};

pub(super) fn interactive_tool_header_line(tool_name: &str, input: Option<&JsonValue>) -> String {
    format!(
        "{ANSI_GREEN}● {ANSI_RESET}{ANSI_BOLD}{}{ANSI_RESET}",
        interactive_tool_header(tool_name, input)
    )
}

pub(super) fn interactive_tool_header(tool_name: &str, input: Option<&JsonValue>) -> String {
    let display = interactive_tool_display_name(tool_name);
    let Some(detail) = input.and_then(|input| interactive_tool_detail(tool_name, input)) else {
        return display;
    };
    format!("{display}({detail})")
}

fn interactive_tool_display_name(tool_name: &str) -> String {
    match tool_name {
        "read_file" => tr("Read"),
        "write_file" => tr("Write"),
        "edit_file" => tr("Edit"),
        "list_files" => tr("List"),
        "glob" | "grep" | "aliyun_doc_search" => tr("Search"),
        "bash" => tr("Bash"),
        "web_fetch" => tr("Fetch"),
        "skill" => tr("Skill"),
        "aliyun_api" => tr("Aliyun API"),
        _ => tool_name.to_owned(),
    }
}

fn interactive_tool_detail(tool_name: &str, input: &JsonValue) -> Option<String> {
    match tool_name {
        "read_file" => {
            let path = json_string_field(input, "path")?;
            let start = json_i64_field(input, "start_line");
            let end = json_i64_field(input, "end_line");
            Some(match (start, end) {
                (Some(start), Some(end)) => {
                    format_interactive_line_range(&format!("{start}-{end}"), None)
                }
                (Some(start), None) => format_interactive_line_range(&format!("{start}-"), None),
                (None, Some(end)) => format_interactive_line_range(&format!("1-{end}"), None),
                (None, None) => path.to_owned(),
            })
        }
        "write_file" | "edit_file" | "list_files" => {
            json_string_field(input, "path").map(ToOwned::to_owned)
        }
        "glob" => json_string_field(input, "pattern").map(ToOwned::to_owned),
        "grep" => json_string_field(input, "pattern").map(ToOwned::to_owned),
        "bash" => json_string_field(input, "command")
            .map(|command| command.lines().next().unwrap_or(command).trim().to_owned())
            .filter(|command| !command.is_empty()),
        "aliyun_api" => interactive_aliyun_api_tool_detail(input),
        "web_fetch" => json_string_field(input, "url").map(ToOwned::to_owned),
        _ => None,
    }
}

pub(super) fn interactive_tool_result_summary(
    tool_name: &str,
    result: &str,
    is_error: bool,
) -> String {
    if tool_name == "aliyun_api" {
        if let Some(summary) = interactive_aliyun_api_result_summary(result, is_error) {
            return summary;
        }
    }
    if is_error {
        return result
            .lines()
            .next()
            .filter(|line| !line.trim().is_empty())
            .unwrap_or("Error")
            .to_owned();
    }
    if tool_name == "write_file" {
        if let Some((lines, path)) = parse_successfully_wrote_result(result) {
            return tr("Successfully wrote {lines} lines to {path}")
                .replace("{lines}", &lines)
                .replace("{path}", &path);
        }
    }
    if tool_name == "read_file" {
        if let Some((range, total)) = parse_read_file_line_range(result) {
            return format_interactive_line_range(&range, total.as_deref());
        }
        if let Some(total) = parse_read_file_total_lines(result) {
            return tr("Read {total} lines").replace("{total}", &total);
        }
    }
    if tool_name == "list_files" {
        if let Some(count) = parse_list_files_item_count(result) {
            return tr("Found {count} items").replace("{count}", &count);
        }
    }
    result
        .lines()
        .next()
        .filter(|line| !line.trim().is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| tr("Worked"))
}

fn interactive_aliyun_api_tool_detail(input: &JsonValue) -> Option<String> {
    let parts = [
        json_string_field(input, "action"),
        json_string_field(input, "product"),
        json_string_field(input, "region_id"),
    ]
    .into_iter()
    .flatten()
    .map(str::trim)
    .filter(|part| !part.is_empty())
    .map(ToOwned::to_owned)
    .collect::<Vec<_>>();
    (!parts.is_empty()).then(|| parts.join(" "))
}

fn interactive_aliyun_api_result_summary(result: &str, is_error: bool) -> Option<String> {
    if is_error {
        return Some(interactive_aliyun_api_error_summary(result));
    }

    let trimmed = result.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(JsonValue::Object(object)) = json::parse(trimmed) {
        if let Some(request_id) =
            json_string_or_number_field_map_any(&object, &["RequestId", "requestId", "request_id"])
        {
            return Some(
                tr("Call succeeded (RequestId: {request_id})").replace("{request_id}", &request_id),
            );
        }
        return Some(tr("Call succeeded"));
    }

    Some(
        tr("Received response ({count} lines)")
            .replace("{count}", &trimmed.lines().count().to_string()),
    )
}

fn interactive_aliyun_api_error_summary(result: &str) -> String {
    if let Some(summary) = parse_interactive_aliyun_http_error_summary(result) {
        return summary;
    }

    interactive_trim_response_suffix(result)
        .lines()
        .next()
        .filter(|line| !line.trim().is_empty())
        .unwrap_or("Error")
        .trim()
        .to_owned()
}

fn parse_interactive_aliyun_http_error_summary(result: &str) -> Option<String> {
    let trimmed = result.trim();
    let (prefix, response) = trimmed.split_once(" Response: ")?;
    let http_status = prefix
        .trim()
        .strip_prefix("HTTP error ")
        .and_then(|status| status.split_whitespace().next())
        .filter(|status| !status.is_empty());
    let JsonValue::Object(object) = json::parse(response.trim()).ok()? else {
        return None;
    };
    let fields = json_object_field_map_any(&object, &["Error", "error"]).unwrap_or(&object);
    let error_code =
        json_string_or_number_field_map_any(fields, &["Code", "code", "ErrorCode", "error_code"]);
    let message = json_string_or_number_field_map_any(
        fields,
        &["Message", "message", "ErrorMessage", "error_message"],
    );
    let request_id = json_string_or_number_field_map_any(
        fields,
        &["RequestId", "RequestID", "requestId", "request_id"],
    );
    if error_code.is_none() && message.is_none() && request_id.is_none() {
        return None;
    }

    let mut summary = if let Some(error_code) = error_code {
        format!("Error: {error_code}")
    } else if let Some(status) = http_status {
        format!("HTTP error {status}")
    } else {
        "Error".to_owned()
    };
    if let Some(status) = http_status {
        summary.push_str(" code: ");
        summary.push_str(status);
    }
    if let Some(message) = message.filter(|message| !message.is_empty()) {
        summary.push_str(", ");
        summary.push_str(&message);
    }
    if let Some(request_id) = request_id.filter(|request_id| !request_id.is_empty()) {
        summary.push_str(" request id: ");
        summary.push_str(&request_id);
    }
    Some(summary)
}

fn interactive_trim_response_suffix(message: &str) -> &str {
    message
        .split_once(" Response: {")
        .map(|(prefix, _)| prefix.trim_end())
        .unwrap_or_else(|| message.trim())
}

fn json_string_or_number_field_map_any(
    object: &BTreeMap<String, JsonValue>,
    keys: &[&str],
) -> Option<String> {
    keys.iter().find_map(|key| match object.get(*key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        Some(JsonValue::Number(value)) => Some(value.clone()),
        _ => None,
    })
}

fn json_object_field_map_any<'a>(
    object: &'a BTreeMap<String, JsonValue>,
    keys: &[&str],
) -> Option<&'a BTreeMap<String, JsonValue>> {
    keys.iter().find_map(|key| match object.get(*key) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    })
}

fn format_interactive_line_range(range: &str, total: Option<&str>) -> String {
    if let Some(total) = total {
        return tr("lines {range} of {total}")
            .replace("{range}", range)
            .replace("{total}", total);
    }
    tr("lines {range}").replace("{range}", range)
}

pub(super) fn interactive_tool_result_has_expandable_detail(
    tool_name: &str,
    result: &str,
    is_error: bool,
) -> bool {
    if is_error {
        return false;
    }
    match tool_name {
        "read_file" | "list_files" | "glob" | "grep" | "bash" | "web_fetch" => {
            result
                .lines()
                .filter(|line| !line.trim().is_empty())
                .take(2)
                .count()
                > 1
        }
        _ => false,
    }
}

fn parse_successfully_wrote_result(result: &str) -> Option<(String, String)> {
    let rest = result.strip_prefix("Successfully wrote ")?;
    let (lines, path) = rest.split_once(" lines to ")?;
    Some((lines.to_owned(), path.trim().to_owned()))
}

fn parse_read_file_total_lines(result: &str) -> Option<String> {
    let first_line = result.lines().next()?.trim();
    let (_, rest) = first_line.split_once(" (")?;
    if let Some((total, _)) = rest.split_once(" lines") {
        return Some(total.to_owned());
    }
    if rest.starts_with("0 lines") {
        return Some("0".to_owned());
    }
    None
}

fn parse_read_file_line_range(result: &str) -> Option<(String, Option<String>)> {
    let first_line = result.lines().next()?.trim();
    let range_start = first_line
        .find("(lines ")
        .map(|index| index + "(lines ".len())
        .or_else(|| {
            first_line
                .find("lines ")
                .map(|index| index + "lines ".len())
        })?;
    let rest = first_line[range_start..].trim_end_matches(')').trim();
    let (range, total) = rest
        .split_once(" of ")
        .map(|(range, total)| (range, Some(total)))
        .unwrap_or((rest, None));
    if range.is_empty() {
        return None;
    }
    Some((range.to_owned(), total.map(ToOwned::to_owned)))
}

fn parse_list_files_item_count(result: &str) -> Option<String> {
    let trimmed = result.trim();
    if trimmed.starts_with("Directory ") && trimmed.ends_with(" is empty.") {
        return Some("0".to_owned());
    }
    let count = result
        .lines()
        .filter(|line| line.starts_with("  ") && !line.trim().is_empty())
        .count();
    (count > 0).then(|| count.to_string())
}

fn json_i64_field(value: &JsonValue, key: &str) -> Option<i64> {
    match json_object_field(value, key) {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}
