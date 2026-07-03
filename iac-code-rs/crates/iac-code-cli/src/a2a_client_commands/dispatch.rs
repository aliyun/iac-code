use crate::a2a_client_args::parse_a2a_client_invocation;
use crate::a2a_client_routes::run_a2a_client_route_preview;
use crate::cli_help::no_such_command_message;

use super::output::{finish_command_result, CommandOutput};
use super::{
    run_a2a_client_call, run_a2a_client_discover, run_a2a_client_extended_card,
    run_a2a_client_push_config_create, run_a2a_client_push_config_delete,
    run_a2a_client_push_config_get, run_a2a_client_push_config_list, run_a2a_client_task_cancel,
    run_a2a_client_task_get, run_a2a_client_task_list, run_a2a_client_task_subscribe,
};

pub(super) fn handle_a2a_client_command(args: &[String]) -> Option<i32> {
    let invocation = match parse_a2a_client_invocation(args) {
        Ok(Some(invocation)) => invocation,
        Ok(None) => return None,
        Err(error) => {
            eprintln!("{error}");
            return Some(1);
        }
    };

    match dispatch_a2a_client_command(&invocation.command, &invocation.args) {
        Some(result) => finish_command_result(
            result,
            CommandOutput::for_a2a_client_command(&invocation.command),
        ),
        None => {
            eprintln!(
                "{}",
                no_such_command_message("iac-code a2a-client", &invocation.command)
            );
            Some(2)
        }
    }
}

pub(super) fn handle_a2a_client_task_command(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a-client") {
        return None;
    }

    let command = args.get(1).map(String::as_str)?;

    dispatch_a2a_client_task_command(Some(command), &args[2..])
        .map(|result| finish_command_result(result, CommandOutput::Always))
        .unwrap_or(None)
}

pub(super) fn handle_a2a_client_push_command(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a-client") {
        return None;
    }

    let command = args.get(1).map(String::as_str)?;

    dispatch_a2a_client_push_command(Some(command), &args[2..])
        .map(|result| finish_command_result(result, CommandOutput::Always))
        .unwrap_or(None)
}

pub(super) fn handle_a2a_client_call(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a-client")
        || args.get(1).map(String::as_str) != Some("call")
    {
        return None;
    }

    finish_command_result(run_a2a_client_call(&args[2..]), CommandOutput::NonEmpty)
}

pub(super) fn handle_a2a_client_discover(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a-client")
        || args.get(1).map(String::as_str) != Some("discover")
    {
        return None;
    }

    finish_command_result(run_a2a_client_discover(&args[2..]), CommandOutput::Always)
}

pub(super) fn handle_a2a_client_route_preview(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a-client")
        || args.get(1).map(String::as_str) != Some("route-preview")
    {
        return None;
    }

    finish_command_result(
        run_a2a_client_route_preview(&args[2..]),
        CommandOutput::Always,
    )
}

fn dispatch_a2a_client_command(command: &str, args: &[String]) -> Option<Result<String, String>> {
    match command {
        "call" => Some(run_a2a_client_call(args)),
        "discover" => Some(run_a2a_client_discover(args)),
        "route-preview" => Some(run_a2a_client_route_preview(args)),
        _ => dispatch_a2a_client_task_command(Some(command), args)
            .or_else(|| dispatch_a2a_client_push_command(Some(command), args)),
    }
}

fn dispatch_a2a_client_task_command(
    command: Option<&str>,
    args: &[String],
) -> Option<Result<String, String>> {
    match command {
        Some("task-get") => Some(run_a2a_client_task_get(args)),
        Some("task-list") => Some(run_a2a_client_task_list(args)),
        Some("task-cancel") => Some(run_a2a_client_task_cancel(args)),
        Some("task-subscribe") => Some(run_a2a_client_task_subscribe(args)),
        _ => None,
    }
}

fn dispatch_a2a_client_push_command(
    command: Option<&str>,
    args: &[String],
) -> Option<Result<String, String>> {
    match command {
        Some("push-config-create") => Some(run_a2a_client_push_config_create(args)),
        Some("push-config-get") => Some(run_a2a_client_push_config_get(args)),
        Some("push-config-list") => Some(run_a2a_client_push_config_list(args)),
        Some("push-config-delete") => Some(run_a2a_client_push_config_delete(args)),
        Some("extended-card") => Some(run_a2a_client_extended_card(args)),
        _ => None,
    }
}
