use std::env;

use iac_code_a2a::server::{validate_server_startup_options, ServerStartupOptions};

use crate::cli_args::non_empty_str;

use super::A2AServerArgs;

pub(crate) fn validate_a2a_server_startup_options_for_cli(
    args: &A2AServerArgs,
) -> Result<String, String> {
    if args.transport == "stdio" && args.log_to_stdout {
        return Err(
            "--log-to-stdout cannot be used with --transport stdio because stdout carries A2A frames."
                .to_owned(),
        );
    }
    validate_server_startup_options(ServerStartupOptions {
        transport: &args.transport,
        socket_path: non_empty_str(&args.socket_path),
        redis_url: non_empty_str(&args.redis_url),
        push_queue: &args.push_queue,
        push_redis_url: non_empty_str(&args.push_redis_url),
        platform: env::consts::OS,
    })
    .map_err(|error| a2a_server_startup_error_message(args, error.to_string()))
}

fn a2a_server_startup_error_message(args: &A2AServerArgs, error: String) -> String {
    if args.config.is_empty() {
        return error;
    }
    match error.as_str() {
        "--socket-path is required for --transport unix." => {
            "socket-path is required in --config for --transport unix.".to_owned()
        }
        "--redis-url is required for --transport redis-streams." => {
            "redis-url is required in --config for --transport redis-streams.".to_owned()
        }
        "--push-redis-url is required for --push-queue redis-streams." => {
            "push-redis-url is required in --config for push-queue: redis-streams.".to_owned()
        }
        _ => error,
    }
}
