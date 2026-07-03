use iac_code_a2a::client::{stream_jsonrpc_payload, A2AClient};
use iac_code_protocol::json::JsonValue;

use crate::a2a_client_args::require_a2a_value;
use crate::a2a_client_format::format_a2a_task_list;
use crate::a2a_client_rpc::{auth_config_from_args, new_cli_request_id, send_a2a_jsonrpc};
use crate::a2a_client_task_args::{
    parse_a2a_task_cancel_args, parse_a2a_task_get_args, parse_a2a_task_list_args,
    parse_a2a_task_subscribe_args,
};
use crate::cli_args::non_empty_str;
use crate::json_utils::format_pretty_json;

pub(super) fn run_a2a_client_task_get(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_task_get_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    let payload =
        A2AClient::get_task_payload(&args.task_id, args.history_length, &new_cli_request_id());
    let response = send_a2a_jsonrpc(&args.url, &payload, args.auth)?;
    Ok(format_pretty_json(&response))
}

pub(super) fn run_a2a_client_task_list(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_task_list_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    if !matches!(args.output.as_str(), "table" | "json") {
        return Err("--output must be table or json.".to_owned());
    }
    let payload = A2AClient::list_tasks_payload(
        non_empty_str(&args.context_id),
        non_empty_str(&args.status),
        args.page_size,
        non_empty_str(&args.page_token),
        args.include_artifacts.then_some(true),
        &new_cli_request_id(),
    );
    let response = send_a2a_jsonrpc(&args.url, &payload, args.auth.clone())?;
    if args.output == "json" {
        Ok(format_pretty_json(&response))
    } else {
        Ok(format_a2a_task_list(
            &response,
            &args.url,
            non_empty_str(&args.context_id),
            non_empty_str(&args.status),
            args.page_size,
            args.include_artifacts,
        ))
    }
}

pub(super) fn run_a2a_client_task_cancel(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_task_cancel_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    let payload = A2AClient::cancel_task_payload(&args.task_id, &new_cli_request_id());
    let response = send_a2a_jsonrpc(&args.url, &payload, args.auth)?;
    Ok(format_pretty_json(&response))
}

pub(super) fn run_a2a_client_task_subscribe(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_task_subscribe_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    let payload = A2AClient::subscribe_task_payload(&args.task_id, &new_cli_request_id());
    let events = stream_jsonrpc_payload(
        &args.url,
        &payload,
        Some(auth_config_from_args(args.auth)),
        None,
    )?;
    Ok(events
        .iter()
        .map(JsonValue::to_compact_json)
        .collect::<Vec<_>>()
        .join("\n"))
}
