use std::io::{self, Write};
use std::path::PathBuf;

use iac_code_a2a::agent_card::{build_agent_card, AgentCardOptions};
use iac_code_a2a::app::{
    resolve_api_key, resolve_api_key_header, resolve_basic_credentials, resolve_token,
    supported_interfaces, A2AAuthConfig as A2AServerAuthConfig, SupportedInterfacesOptions,
};
use iac_code_a2a::artifacts::A2AArtifactStore;
use iac_code_a2a::exposure::{normalize_a2a_exposure_tokens, normalize_a2a_exposure_types};
use iac_code_a2a::push::A2APushConfigStore;
use iac_code_a2a::task_store::A2ATaskStore;
use iac_code_config::paths::ConfigPaths;
use iac_code_protocol::json::JsonValue;

use crate::a2a_push::{build_a2a_push_queue, A2APushQueueRuntime};
use crate::a2a_server_args::A2AServerArgs;
use crate::cli_args::{non_empty_str, non_empty_string};

pub(super) struct A2AServerRuntime {
    pub(super) card: JsonValue,
    pub(super) auth: A2AServerAuthConfig,
    pub(super) task_store: A2ATaskStore,
    pub(super) push_config_store: A2APushConfigStore,
    pub(super) push_queue: Option<A2APushQueueRuntime>,
    pub(super) artifact_store: A2AArtifactStore,
    pub(super) auto_approve_permissions: bool,
    pub(super) log_to_stdout: bool,
}

impl A2AServerRuntime {
    pub(super) fn card(&self) -> &JsonValue {
        &self.card
    }

    #[cfg(test)]
    pub(super) fn logs_to_stdout(&self) -> bool {
        self.log_to_stdout
    }
}

pub(super) fn log_a2a_server_error(log_to_stdout: bool, message: &str) {
    eprintln!("{message}");
    let stdout = io::stdout();
    let mut stdout = stdout.lock();
    let _ = write_a2a_server_log_to_stdout(log_to_stdout, message, &mut stdout);
}

pub(super) fn write_a2a_server_log_to_stdout<W: Write>(
    log_to_stdout: bool,
    message: &str,
    writer: &mut W,
) -> io::Result<()> {
    if log_to_stdout {
        writer.write_all(message.as_bytes())?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    Ok(())
}

pub(super) fn build_a2a_server_runtime(
    args: &A2AServerArgs,
    transport: &str,
) -> Result<A2AServerRuntime, String> {
    let token = resolve_token(non_empty_str(&args.token));
    let basic = resolve_basic_credentials(
        non_empty_str(&args.basic_username),
        non_empty_str(&args.basic_password),
    );
    let api_key = resolve_api_key(non_empty_str(&args.api_key));
    let api_key_header = resolve_api_key_header(Some(&args.api_key_header));
    let auth = A2AServerAuthConfig::new(
        token.as_deref(),
        basic.as_ref().map(|(username, _)| username.as_str()),
        basic.as_ref().map(|(_, password)| password.as_str()),
        api_key.as_deref(),
        &api_key_header,
    );

    let mut card_options = AgentCardOptions::new(&args.host, args.port, false);
    card_options.token_enabled = auth.token.is_some();
    card_options.basic_enabled = auth.basic_username.is_some() && auth.basic_password.is_some();
    card_options.api_key_enabled = auth.api_key.is_some();
    card_options.api_key_header = api_key_header;
    card_options.push_notifications = args.push_notifications;
    card_options.signing_secret = non_empty_string(args.signing_secret.clone());
    let thinking_exposure_types = if args.thinking_exposure.is_empty() {
        normalize_a2a_exposure_types(None)
    } else {
        normalize_a2a_exposure_tokens(args.thinking_exposure.iter().map(String::as_str))
    };
    card_options.thinking_exposure_types = thinking_exposure_types
        .map_err(|error| error.to_string())?
        .into_iter()
        .collect();
    card_options.supported_interfaces = supported_interfaces(SupportedInterfacesOptions {
        transport: transport.to_owned(),
        host: args.host.clone(),
        port: args.port,
        socket_path: non_empty_string(args.socket_path.clone()),
        ws_path: args.ws_path.clone(),
        grpc_host: non_empty_string(args.grpc_host.clone()),
        grpc_port: args.grpc_port,
        redis_url: non_empty_string(args.redis_url.clone()),
        request_stream: args.request_stream.clone(),
        response_stream: args.response_stream.clone(),
        consumer_group: args.consumer_group.clone(),
    })
    .unwrap_or_default();
    let card = build_agent_card(card_options);
    let task_store = A2ATaskStore::new();
    let persistence_root = non_empty_string(args.persistence_dir.clone()).map_or_else(
        || {
            ConfigPaths::from_env()
                .map(|paths| paths.subdirs().a2a)
                .map_err(|error| error.to_string())
        },
        |path| Ok(PathBuf::from(path)),
    )?;
    let artifact_root = non_empty_string(args.artifact_dir.clone())
        .map(PathBuf::from)
        .unwrap_or_else(|| persistence_root.join("artifacts"));
    let push_config_store = A2APushConfigStore::new(&persistence_root);
    let push_queue = build_a2a_push_queue(args, &persistence_root)?;
    let artifact_store = A2AArtifactStore::new(artifact_root);
    Ok(A2AServerRuntime {
        card,
        auth,
        task_store,
        push_config_store,
        push_queue,
        artifact_store,
        auto_approve_permissions: args.auto_approve_permissions,
        log_to_stdout: args.log_to_stdout,
    })
}
