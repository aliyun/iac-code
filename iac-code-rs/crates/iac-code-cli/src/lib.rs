mod a2a_artifacts;
mod a2a_client_args;
mod a2a_client_commands;
mod a2a_client_format;
mod a2a_client_route_args;
mod a2a_client_routes;
mod a2a_client_rpc;
mod a2a_client_task_args;
mod a2a_config;
mod a2a_config_yaml;
mod a2a_grpc_convert;
mod a2a_grpc_json;
mod a2a_grpc_proto;
mod a2a_messages;
mod a2a_payload;
mod a2a_push;
mod a2a_redis;
mod a2a_redis_parse;
mod a2a_response;
mod a2a_server;
mod a2a_server_args;
mod a2a_server_dispatch;
mod a2a_server_grpc;
mod a2a_server_grpc_jsonrpc;
mod a2a_server_grpc_official;
mod a2a_server_http;
mod a2a_server_redis_streams;
mod a2a_server_runtime;
mod a2a_server_stdio;
mod a2a_server_unix;
mod a2a_server_websocket;
mod a2a_tasks;
mod acp_agent;
mod acp_http;
mod acp_payload;
mod acp_resume;
mod acp_server;
mod acp_server_args;
mod acp_sessions;
mod acp_stdio;
mod acp_stdio_client;
mod ansi;
mod cli_a2a_client_help;
mod cli_args;
mod cli_completion;
mod cli_help;
mod cli_i18n;
mod cli_protocol_help;
mod cli_runtime;
mod debug_logging;
mod headless_executor;
mod headless_fake_runner;
mod headless_runner;
mod headless_subagent;
mod headless_usage;
mod interactive_banner;
mod interactive_commands;
mod interactive_compact_command;
mod interactive_debug_command;
mod interactive_markdown;
mod interactive_memory_commands;
mod interactive_prompt_handler;
mod interactive_provider_commands;
mod interactive_rename_command;
mod interactive_renderer;
mod interactive_resume_command;
mod interactive_runtime;
mod interactive_session;
mod interactive_shell_escape;
mod interactive_skill_invocation;
mod interactive_skills;
mod interactive_status;
mod interactive_tool_renderer;
mod interactive_usage;
mod interactive_working;
mod json_utils;
mod jsonrpc_payload;
mod permission_settings;
mod prompt_content;
mod provider_config;
#[cfg(unix)]
mod raw_auth;
#[cfg(unix)]
mod raw_auth_cloud;
#[cfg(unix)]
mod raw_auth_cloud_fields;
#[cfg(unix)]
mod raw_auth_cloud_oauth;
#[cfg(unix)]
mod raw_auth_input;
#[cfg(unix)]
mod raw_auth_llm;
#[cfg(unix)]
mod raw_auth_oauth;
#[cfg(unix)]
mod raw_auth_oauth_browser;
#[cfg(unix)]
mod raw_auth_oauth_callback;
#[cfg(unix)]
mod raw_auth_oauth_client;
#[cfg(unix)]
mod raw_auth_oauth_fake;
#[cfg(unix)]
mod raw_auth_oauth_render;
#[cfg(unix)]
mod raw_auth_oauth_types;
#[cfg(unix)]
mod raw_auth_oauth_utils;
#[cfg(unix)]
mod raw_effort;
mod raw_memory;
#[cfg(unix)]
mod raw_model_context;
#[cfg(unix)]
mod raw_model_effort;
#[cfg(unix)]
mod raw_picker;
#[cfg(unix)]
mod raw_prompt_commands;
#[cfg(unix)]
mod raw_prompt_context;
#[cfg(unix)]
mod raw_prompt_images;
#[cfg(unix)]
mod raw_prompt_input;
#[cfg(unix)]
mod raw_prompt_render_clear;
#[cfg(unix)]
mod raw_prompt_render_state;
#[cfg(unix)]
mod raw_prompt_render_suggestions;
#[cfg(unix)]
mod raw_prompt_renderer;
#[cfg(unix)]
mod raw_prompt_submit;
#[cfg(unix)]
mod raw_prompt_text;
#[cfg(unix)]
mod raw_rename;
#[cfg(unix)]
mod raw_resume;
#[cfg(unix)]
mod raw_resume_preview;
#[cfg(unix)]
mod raw_search;
#[cfg(unix)]
mod raw_select;
#[cfg(unix)]
mod raw_skills;
#[cfg(unix)]
mod raw_suggestions;
#[cfg(unix)]
mod raw_transcript;
mod session_utils;
mod skills_management;
#[cfg(test)]
pub(crate) mod test_support;
mod wire;
mod yaml_config;

pub fn run_cli(args: impl IntoIterator<Item = String>) -> i32 {
    cli_runtime::run_cli(args)
}

#[cfg(test)]
mod tests;
