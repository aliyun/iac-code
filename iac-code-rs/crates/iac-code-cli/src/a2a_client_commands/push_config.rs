use iac_code_a2a::client::{A2AClient, PushConfigRequest};

use crate::a2a_client_args::require_a2a_value;
use crate::a2a_client_rpc::{new_cli_request_id, post_a2a_jsonrpc, push_callback_authentication};
use crate::a2a_client_task_args::{
    parse_a2a_extended_card_args, parse_a2a_push_config_create_args,
    parse_a2a_push_config_delete_args, parse_a2a_push_config_get_args,
    parse_a2a_push_config_list_args,
};
use crate::cli_args::non_empty_str;

pub(super) fn run_a2a_client_push_config_create(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_push_config_create_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    require_a2a_value(&args.config_id, "--config-id", "config-id")?;
    require_a2a_value(&args.callback_url, "--callback-url", "callback-url")?;
    let payload = A2AClient::create_push_notification_config_payload(
        PushConfigRequest {
            task_id: &args.task_id,
            config_id: &args.config_id,
            url: &args.callback_url,
            token: non_empty_str(&args.notification_token),
            authentication: push_callback_authentication(&args.auth_scheme, &args.auth_credentials),
        },
        &new_cli_request_id(),
    );
    post_a2a_jsonrpc(&args.url, &payload, args.auth)
}

pub(super) fn run_a2a_client_push_config_get(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_push_config_get_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    require_a2a_value(&args.config_id, "--config-id", "config-id")?;
    let payload = A2AClient::get_push_notification_config_payload(
        &args.task_id,
        &args.config_id,
        &new_cli_request_id(),
    );
    post_a2a_jsonrpc(&args.url, &payload, args.auth)
}

pub(super) fn run_a2a_client_push_config_list(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_push_config_list_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    let payload = A2AClient::list_push_notification_configs_payload(
        &args.task_id,
        args.page_size,
        non_empty_str(&args.page_token),
        &new_cli_request_id(),
    );
    post_a2a_jsonrpc(&args.url, &payload, args.auth)
}

pub(super) fn run_a2a_client_push_config_delete(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_push_config_delete_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    require_a2a_value(&args.task_id, "--task-id", "task-id")?;
    require_a2a_value(&args.config_id, "--config-id", "config-id")?;
    let payload = A2AClient::delete_push_notification_config_payload(
        &args.task_id,
        &args.config_id,
        &new_cli_request_id(),
    );
    post_a2a_jsonrpc(&args.url, &payload, args.auth)
}

pub(super) fn run_a2a_client_extended_card(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_extended_card_args(args)?;
    require_a2a_value(&args.url, "--url", "url")?;
    let payload = A2AClient::get_extended_agent_card_payload(&new_cli_request_id());
    post_a2a_jsonrpc(&args.url, &payload, args.auth)
}
