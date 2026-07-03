use super::a2a_config::{apply_a2a_client_config, load_a2a_client_config};
use super::cli_args::next_option_value;

#[derive(Clone, Debug)]
pub(super) struct A2ADiscoverArgs {
    pub(super) url: String,
    pub(super) token: String,
    pub(super) basic_username: String,
    pub(super) basic_password: String,
    pub(super) api_key: String,
    pub(super) api_key_header: String,
    pub(super) verify_card_secret: String,
    pub(super) verify_card_jwks_url: String,
    pub(super) require_card_signature: bool,
}

#[derive(Clone, Debug)]
pub(super) struct A2ACallArgs {
    pub(super) url: String,
    pub(super) routes: Vec<String>,
    pub(super) route_name: String,
    pub(super) prompt: String,
    pub(super) cwd: String,
    pub(super) context_id: String,
    pub(super) model: String,
    pub(super) token: String,
    pub(super) basic_username: String,
    pub(super) basic_password: String,
    pub(super) api_key: String,
    pub(super) api_key_header: String,
    pub(super) verify_card_secret: String,
    pub(super) verify_card_jwks_url: String,
    pub(super) require_card_signature: bool,
    pub(super) timeout_seconds: f64,
    pub(super) stream: bool,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2AClientInvocation {
    pub(super) command: String,
    pub(super) args: Vec<String>,
}

impl Default for A2ACallArgs {
    fn default() -> Self {
        Self {
            url: String::new(),
            routes: Vec::new(),
            route_name: String::new(),
            prompt: String::new(),
            cwd: ".".to_owned(),
            context_id: String::new(),
            model: String::new(),
            token: String::new(),
            basic_username: String::new(),
            basic_password: String::new(),
            api_key: String::new(),
            api_key_header: "X-API-Key".to_owned(),
            verify_card_secret: String::new(),
            verify_card_jwks_url: String::new(),
            require_card_signature: false,
            timeout_seconds: 30.0,
            stream: false,
        }
    }
}

impl Default for A2ADiscoverArgs {
    fn default() -> Self {
        Self {
            api_key_header: "X-API-Key".to_owned(),
            url: String::new(),
            token: String::new(),
            basic_username: String::new(),
            basic_password: String::new(),
            api_key: String::new(),
            verify_card_secret: String::new(),
            verify_card_jwks_url: String::new(),
            require_card_signature: false,
        }
    }
}

pub(super) fn parse_a2a_client_invocation(
    args: &[String],
) -> Result<Option<A2AClientInvocation>, String> {
    if args.first().map(String::as_str) != Some("a2a-client") {
        return Ok(None);
    }

    let mut index = 1usize;
    let mut config_path = String::new();
    while index < args.len() {
        match args[index].as_str() {
            "--config" => config_path = next_option_value(args, &mut index, "--config")?,
            "--help" | "-h" => return Ok(None),
            option if option.starts_with('-') => return Err(format!("No such option: {option}")),
            command => {
                let config = load_a2a_client_config(&config_path)?;
                let command_args =
                    apply_a2a_client_config(command, args[index + 1..].to_vec(), &config);
                return Ok(Some(A2AClientInvocation {
                    command: command.to_owned(),
                    args: command_args,
                }));
            }
        }
    }
    Ok(None)
}

pub(super) fn parse_a2a_call_args(args: &[String]) -> Result<A2ACallArgs, String> {
    let mut parsed = A2ACallArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--route" => parsed
                .routes
                .push(next_option_value(args, &mut index, "--route")?),
            "--route-name" => {
                parsed.route_name = next_option_value(args, &mut index, "--route-name")?;
            }
            "--prompt" | "-p" => {
                parsed.prompt = next_option_value(args, &mut index, "--prompt")?;
            }
            "--cwd" => parsed.cwd = next_option_value(args, &mut index, "--cwd")?,
            "--context-id" => {
                parsed.context_id = next_option_value(args, &mut index, "--context-id")?;
            }
            "--model" => {
                parsed.model = next_option_value(args, &mut index, "--model")?;
            }
            "--token" => parsed.token = next_option_value(args, &mut index, "--token")?,
            "--basic-username" => {
                parsed.basic_username = next_option_value(args, &mut index, "--basic-username")?;
            }
            "--basic-password" => {
                parsed.basic_password = next_option_value(args, &mut index, "--basic-password")?;
            }
            "--api-key" => parsed.api_key = next_option_value(args, &mut index, "--api-key")?,
            "--api-key-header" => {
                parsed.api_key_header = next_option_value(args, &mut index, "--api-key-header")?;
            }
            "--verify-card-secret" | "--signing-secret" => {
                parsed.verify_card_secret =
                    next_option_value(args, &mut index, "--verify-card-secret")?;
            }
            "--verify-card-jwks-url" => {
                parsed.verify_card_jwks_url =
                    next_option_value(args, &mut index, "--verify-card-jwks-url")?;
            }
            "--require-card-signature" | "--require-signature" => {
                parsed.require_card_signature = true;
                index += 1;
            }
            "--timeout" => {
                let value = next_option_value(args, &mut index, "--timeout")?;
                parsed.timeout_seconds = value
                    .parse::<f64>()
                    .map_err(|_| format!("Invalid --timeout '{}'.", value))?;
            }
            "--stream" => {
                parsed.stream = true;
                index += 1;
            }
            other => {
                return Err(format!("No such option: {other}"));
            }
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_discover_args(args: &[String]) -> Result<A2ADiscoverArgs, String> {
    let mut parsed = A2ADiscoverArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--token" => parsed.token = next_option_value(args, &mut index, "--token")?,
            "--basic-username" => {
                parsed.basic_username = next_option_value(args, &mut index, "--basic-username")?;
            }
            "--basic-password" => {
                parsed.basic_password = next_option_value(args, &mut index, "--basic-password")?;
            }
            "--api-key" => parsed.api_key = next_option_value(args, &mut index, "--api-key")?,
            "--api-key-header" => {
                parsed.api_key_header = next_option_value(args, &mut index, "--api-key-header")?;
            }
            "--verify-card-secret" | "--signing-secret" => {
                parsed.verify_card_secret =
                    next_option_value(args, &mut index, "--verify-card-secret")?;
            }
            "--verify-card-jwks-url" => {
                parsed.verify_card_jwks_url =
                    next_option_value(args, &mut index, "--verify-card-jwks-url")?;
            }
            "--require-card-signature" | "--require-signature" => {
                parsed.require_card_signature = true;
                index += 1;
            }
            other => {
                return Err(format!("No such option: {other}"));
            }
        }
    }
    Ok(parsed)
}

pub(super) fn require_a2a_value(
    value: &str,
    option_name: &str,
    config_name: &str,
) -> Result<(), String> {
    if value.is_empty() {
        Err(format!(
            "{config_name} is required. Provide {option_name} or {config_name} in --config."
        ))
    } else {
        Ok(())
    }
}
