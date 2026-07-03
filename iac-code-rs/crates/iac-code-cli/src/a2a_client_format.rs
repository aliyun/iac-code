use iac_code_a2a::client::extract_response_text;
use iac_code_a2a::router::A2ARoute;
use iac_code_protocol::json::JsonValue;

use super::json_utils::{
    format_pretty_json, json_object_field, json_string, json_string_field, json_string_or_empty,
};
use super::session_utils::shell_quote;

pub(super) fn format_a2a_route_json(route: &A2ARoute) -> String {
    format!(
        "{{\n  \"name\": {},\n  \"skills\": {},\n  \"tags\": {},\n  \"url\": {}\n}}",
        json_string(&route.name),
        format_json_string_array(&route.skills),
        format_json_string_array(&route.tags),
        json_string(&route.url)
    )
}

fn format_json_string_array(values: &[String]) -> String {
    if values.is_empty() {
        return "[]".to_owned();
    }
    format!(
        "[\n{}\n  ]",
        values
            .iter()
            .map(|value| format!("    {}", json_string(value)))
            .collect::<Vec<_>>()
            .join(",\n")
    )
}

pub(super) fn format_a2a_task_list(
    response: &JsonValue,
    url: &str,
    context_id: Option<&str>,
    status: Option<&str>,
    page_size: Option<u64>,
    include_artifacts: bool,
) -> String {
    let Some(result) = json_object_field(response, "result") else {
        return format_pretty_json(response);
    };
    let tasks = match json_object_field(result, "tasks") {
        Some(JsonValue::Array(tasks)) => tasks.as_slice(),
        _ => &[],
    };
    if tasks.is_empty() {
        return "No A2A tasks found.".to_owned();
    }

    let mut rows = vec![vec![
        "ID".to_owned(),
        "Status".to_owned(),
        "Context".to_owned(),
        "Updated".to_owned(),
        "Message".to_owned(),
    ]];
    for item in tasks {
        let status_obj = json_object_field(item, "status");
        rows.push(vec![
            clip(json_string_or_empty(item, "id"), 28),
            friendly_task_state(status_obj.and_then(|value| json_string_field(value, "state"))),
            clip(
                json_string_field(item, "contextId")
                    .or_else(|| json_string_field(item, "context_id"))
                    .unwrap_or_default(),
                22,
            ),
            clip(
                status_obj
                    .and_then(|value| json_string_field(value, "timestamp"))
                    .unwrap_or_default(),
                20,
            ),
            clip(
                status_obj
                    .and_then(|value| json_object_field(value, "message"))
                    .map(extract_a2a_message_text)
                    .unwrap_or_default(),
                56,
            ),
        ]);
    }

    let mut lines = vec![
        render_table(&rows),
        format_a2a_task_list_summary(result, tasks.len()),
    ];
    if let Some(next_token) = json_string_field(result, "nextPageToken")
        .or_else(|| json_string_field(result, "next_page_token"))
        .filter(|value| !value.is_empty())
    {
        lines.push(format!(
            "Next page: {}",
            format_a2a_task_list_next_command(
                url,
                context_id,
                status,
                page_size,
                next_token,
                include_artifacts
            )
        ));
    }
    lines.join("\n")
}

pub(super) fn format_a2a_stream_event(event: &JsonValue) -> String {
    let text = extract_response_text(event);
    if text.is_empty() {
        event.to_compact_json()
    } else {
        text
    }
}

fn format_a2a_task_list_summary(result: &JsonValue, shown: usize) -> String {
    match json_object_field(result, "totalSize").or_else(|| json_object_field(result, "total_size"))
    {
        Some(JsonValue::Number(total)) => format!("Showing {shown} of {total} tasks."),
        _ => format!("Showing {shown} tasks."),
    }
}

fn format_a2a_task_list_next_command(
    url: &str,
    context_id: Option<&str>,
    status: Option<&str>,
    page_size: Option<u64>,
    page_token: &str,
    include_artifacts: bool,
) -> String {
    let mut parts = vec![
        "iac-code".to_owned(),
        "a2a-client".to_owned(),
        "task-list".to_owned(),
        "--url".to_owned(),
        url.to_owned(),
    ];
    if let Some(context_id) = context_id {
        parts.extend(["--context-id".to_owned(), context_id.to_owned()]);
    }
    if let Some(status) = status {
        parts.extend(["--status".to_owned(), status.to_owned()]);
    }
    if let Some(page_size) = page_size {
        parts.extend(["--page-size".to_owned(), page_size.to_string()]);
    }
    if include_artifacts {
        parts.push("--include-artifacts".to_owned());
    }
    parts.extend(["--page-token".to_owned(), page_token.to_owned()]);
    parts
        .iter()
        .map(|part| shell_quote(part))
        .collect::<Vec<_>>()
        .join(" ")
}

fn render_table(rows: &[Vec<String>]) -> String {
    let column_count = rows.first().map(Vec::len).unwrap_or(0);
    let widths = (0..column_count)
        .map(|index| rows.iter().map(|row| row[index].len()).max().unwrap_or(0))
        .collect::<Vec<_>>();
    let mut rendered = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        rendered.push(render_table_row(row, &widths));
        if row_index == 0 {
            rendered.push(
                widths
                    .iter()
                    .map(|width| "-".repeat(*width))
                    .collect::<Vec<_>>()
                    .join("  ")
                    .trim_end()
                    .to_owned(),
            );
        }
    }
    rendered.join("\n")
}

fn render_table_row(row: &[String], widths: &[usize]) -> String {
    row.iter()
        .enumerate()
        .map(|(index, value)| format!("{value:<width$}", width = widths[index]))
        .collect::<Vec<_>>()
        .join("  ")
        .trim_end()
        .to_owned()
}

fn friendly_task_state(value: Option<&str>) -> String {
    let value = value.unwrap_or_default();
    value
        .strip_prefix("TASK_STATE_")
        .unwrap_or(value)
        .to_ascii_lowercase()
        .replace('_', "-")
}

fn clip(value: impl AsRef<str>, limit: usize) -> String {
    let value = value.as_ref();
    if value.len() <= limit {
        value.to_owned()
    } else {
        format!("{}...", &value[..limit - 3])
    }
}

pub(super) fn extract_a2a_message_text(message: &JsonValue) -> String {
    let Some(JsonValue::Array(parts)) = json_object_field(message, "parts") else {
        return String::new();
    };
    parts
        .iter()
        .filter_map(|part| json_string_field(part, "text"))
        .collect::<Vec<_>>()
        .join("")
}
