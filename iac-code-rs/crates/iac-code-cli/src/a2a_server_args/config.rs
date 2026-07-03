use crate::a2a_config::{
    apply_config_optional_u16, apply_config_string, apply_config_u16, apply_config_u64,
    config_values, yaml_bool_value, A2AClientConfig,
};

use super::A2AServerArgs;

pub(super) fn apply_a2a_server_config(args: &mut A2AServerArgs, config: &A2AClientConfig) {
    apply_config_string(&mut args.host, config, "host");
    apply_config_u16(&mut args.port, config, "port");
    apply_config_string(&mut args.transport, config, "transport");
    apply_config_string(&mut args.socket_path, config, "socket_path");
    apply_config_string(&mut args.ws_path, config, "ws_path");
    apply_config_string(&mut args.grpc_host, config, "grpc_host");
    apply_config_optional_u16(&mut args.grpc_port, config, "grpc_port");
    apply_config_string(&mut args.redis_url, config, "redis_url");
    apply_config_string(&mut args.request_stream, config, "request_stream");
    apply_config_string(&mut args.response_stream, config, "response_stream");
    apply_config_string(&mut args.consumer_group, config, "consumer_group");
    apply_config_string(&mut args.token, config, "token");
    apply_config_string(&mut args.basic_username, config, "basic_username");
    apply_config_string(&mut args.basic_password, config, "basic_password");
    apply_config_string(&mut args.api_key, config, "api_key");
    apply_config_string(&mut args.push_queue, config, "push_queue");
    apply_config_string(&mut args.push_redis_url, config, "push_redis_url");
    apply_config_string(&mut args.push_stream, config, "push_stream");
    apply_config_string(&mut args.push_retry_key, config, "push_retry_key");
    apply_config_string(&mut args.push_dead_stream, config, "push_dead_stream");
    apply_config_string(&mut args.push_consumer_group, config, "push_consumer_group");
    apply_config_string(&mut args.push_consumer_name, config, "push_consumer_name");
    apply_config_u64(
        &mut args.push_lease_timeout_ms,
        config,
        "push_lease_timeout_ms",
    );
    apply_config_string(&mut args.api_key_header, config, "api_key_header");
    apply_config_string(&mut args.signing_secret, config, "signing_secret");
    apply_config_string(&mut args.persistence_dir, config, "persistence_dir");
    apply_config_string(&mut args.artifact_dir, config, "artifact_dir");
    let thinking_exposure = config_values(config, "thinking_exposure")
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();
    if !thinking_exposure.is_empty() {
        args.thinking_exposure = thinking_exposure;
    }
    if config
        .get("push_notifications")
        .is_some_and(|value| yaml_bool_value(value))
    {
        args.push_notifications = true;
    }
    if config
        .get("auto_approve_permissions")
        .is_some_and(|value| yaml_bool_value(value))
    {
        args.auto_approve_permissions = true;
    }
    if config
        .get("log_to_stdout")
        .is_some_and(|value| yaml_bool_value(value))
    {
        args.log_to_stdout = true;
    }
}
