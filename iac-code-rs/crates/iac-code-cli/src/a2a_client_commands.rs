mod call;
mod discover;
mod dispatch;
mod output;
mod push_config;
mod task;

pub(super) fn handle_a2a_client_command(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_command(args)
}

pub(super) fn handle_a2a_client_task_command(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_task_command(args)
}

pub(super) fn handle_a2a_client_push_command(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_push_command(args)
}

pub(super) fn handle_a2a_client_call(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_call(args)
}

pub(super) fn handle_a2a_client_discover(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_discover(args)
}

pub(super) fn handle_a2a_client_route_preview(args: &[String]) -> Option<i32> {
    dispatch::handle_a2a_client_route_preview(args)
}

pub(super) fn run_a2a_client_call(args: &[String]) -> Result<String, String> {
    call::run_a2a_client_call(args)
}

pub(super) fn run_a2a_client_discover(args: &[String]) -> Result<String, String> {
    discover::run_a2a_client_discover(args)
}

pub(super) fn run_a2a_client_task_get(args: &[String]) -> Result<String, String> {
    task::run_a2a_client_task_get(args)
}

pub(super) fn run_a2a_client_task_list(args: &[String]) -> Result<String, String> {
    task::run_a2a_client_task_list(args)
}

pub(super) fn run_a2a_client_task_cancel(args: &[String]) -> Result<String, String> {
    task::run_a2a_client_task_cancel(args)
}

pub(super) fn run_a2a_client_task_subscribe(args: &[String]) -> Result<String, String> {
    task::run_a2a_client_task_subscribe(args)
}

pub(super) fn run_a2a_client_push_config_create(args: &[String]) -> Result<String, String> {
    push_config::run_a2a_client_push_config_create(args)
}

pub(super) fn run_a2a_client_push_config_get(args: &[String]) -> Result<String, String> {
    push_config::run_a2a_client_push_config_get(args)
}

pub(super) fn run_a2a_client_push_config_list(args: &[String]) -> Result<String, String> {
    push_config::run_a2a_client_push_config_list(args)
}

pub(super) fn run_a2a_client_push_config_delete(args: &[String]) -> Result<String, String> {
    push_config::run_a2a_client_push_config_delete(args)
}

pub(super) fn run_a2a_client_extended_card(args: &[String]) -> Result<String, String> {
    push_config::run_a2a_client_extended_card(args)
}
