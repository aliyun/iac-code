use crate::a2a_config::load_a2a_client_config;
use crate::cli_args::next_option_value;

use super::config::apply_a2a_server_config;
use super::A2AServerArgs;

pub(crate) fn parse_a2a_server_args(args: &[String]) -> Result<A2AServerArgs, String> {
    let mut parsed = A2AServerArgs::default();
    let mut config_path = String::new();
    let mut scan_index = 0usize;
    while scan_index < args.len() {
        if args[scan_index] == "--config" {
            config_path = next_option_value(args, &mut scan_index, "--config")?;
        } else {
            scan_index += 1;
        }
    }
    if !config_path.is_empty() {
        parsed.config = config_path;
        let config = load_a2a_client_config(&parsed.config)?;
        apply_a2a_server_config(&mut parsed, &config);
    }

    let mut index = 0usize;
    let mut thinking_exposure_cli_seen = false;
    while index < args.len() {
        match args[index].as_str() {
            "--config" => parsed.config = next_option_value(args, &mut index, "--config")?,
            "--host" => parsed.host = next_option_value(args, &mut index, "--host")?,
            "--port" => {
                let value = next_option_value(args, &mut index, "--port")?;
                parsed.port = value
                    .parse::<u16>()
                    .map_err(|_| format!("Invalid --port '{}'.", value))?;
            }
            "--transport" => {
                parsed.transport = next_option_value(args, &mut index, "--transport")?;
            }
            "--socket-path" => {
                parsed.socket_path = next_option_value(args, &mut index, "--socket-path")?;
            }
            "--redis-url" => {
                parsed.redis_url = next_option_value(args, &mut index, "--redis-url")?;
            }
            "--push-queue" => {
                parsed.push_queue = next_option_value(args, &mut index, "--push-queue")?;
            }
            "--push-redis-url" => {
                parsed.push_redis_url = next_option_value(args, &mut index, "--push-redis-url")?;
            }
            "--push-stream" => {
                parsed.push_stream = next_option_value(args, &mut index, "--push-stream")?;
            }
            "--push-retry-key" => {
                parsed.push_retry_key = next_option_value(args, &mut index, "--push-retry-key")?;
            }
            "--push-dead-stream" => {
                parsed.push_dead_stream =
                    next_option_value(args, &mut index, "--push-dead-stream")?;
            }
            "--push-consumer-group" => {
                parsed.push_consumer_group =
                    next_option_value(args, &mut index, "--push-consumer-group")?;
            }
            "--push-consumer-name" => {
                parsed.push_consumer_name =
                    next_option_value(args, &mut index, "--push-consumer-name")?;
            }
            "--push-lease-timeout-ms" => {
                let value = next_option_value(args, &mut index, "--push-lease-timeout-ms")?;
                parsed.push_lease_timeout_ms = value
                    .parse::<u64>()
                    .map_err(|_| format!("Invalid --push-lease-timeout-ms '{}'.", value))?;
            }
            "--api-key-header" => {
                parsed.api_key_header = next_option_value(args, &mut index, "--api-key-header")?;
            }
            "--push-notifications" => {
                parsed.push_notifications = true;
                index += 1;
            }
            "--log-to-stdout" => {
                parsed.log_to_stdout = true;
                index += 1;
            }
            "--no-log-to-stdout" => {
                parsed.log_to_stdout = false;
                index += 1;
            }
            "--signing-secret" => {
                parsed.signing_secret = next_option_value(args, &mut index, "--signing-secret")?;
            }
            "--thinking-exposure" | "--debug" | "-d" => {
                if args[index].as_str() == "--thinking-exposure" {
                    if !thinking_exposure_cli_seen {
                        parsed.thinking_exposure.clear();
                        thinking_exposure_cli_seen = true;
                    }
                    parsed.thinking_exposure.push(next_option_value(
                        args,
                        &mut index,
                        "--thinking-exposure",
                    )?);
                } else {
                    index += 1;
                }
            }
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}
