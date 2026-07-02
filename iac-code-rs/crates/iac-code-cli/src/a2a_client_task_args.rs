use super::cli_args::next_option_value;

#[derive(Clone, Debug)]
pub(super) struct A2AClientAuthArgs {
    pub(super) token: String,
    pub(super) basic_username: String,
    pub(super) basic_password: String,
    pub(super) api_key: String,
    pub(super) api_key_header: String,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2ATaskGetArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) history_length: Option<u64>,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug)]
pub(super) struct A2ATaskListArgs {
    pub(super) url: String,
    pub(super) context_id: String,
    pub(super) status: String,
    pub(super) page_size: Option<u64>,
    pub(super) page_token: String,
    pub(super) include_artifacts: bool,
    pub(super) output: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2ATaskCancelArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2ATaskSubscribeArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2APushConfigCreateArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) config_id: String,
    pub(super) callback_url: String,
    pub(super) notification_token: String,
    pub(super) auth_scheme: String,
    pub(super) auth_credentials: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2APushConfigGetArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) config_id: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2APushConfigListArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) page_size: Option<u64>,
    pub(super) page_token: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2APushConfigDeleteArgs {
    pub(super) url: String,
    pub(super) task_id: String,
    pub(super) config_id: String,
    pub(super) auth: A2AClientAuthArgs,
}

#[derive(Clone, Debug, Default)]
pub(super) struct A2AExtendedCardArgs {
    pub(super) url: String,
    pub(super) auth: A2AClientAuthArgs,
}

impl Default for A2AClientAuthArgs {
    fn default() -> Self {
        Self {
            api_key_header: "X-API-Key".to_owned(),
            token: String::new(),
            basic_username: String::new(),
            basic_password: String::new(),
            api_key: String::new(),
        }
    }
}

impl Default for A2ATaskListArgs {
    fn default() -> Self {
        Self {
            output: "table".to_owned(),
            url: String::new(),
            context_id: String::new(),
            status: String::new(),
            page_size: None,
            page_token: String::new(),
            include_artifacts: false,
            auth: A2AClientAuthArgs::default(),
        }
    }
}

pub(super) fn parse_a2a_task_get_args(args: &[String]) -> Result<A2ATaskGetArgs, String> {
    let mut parsed = A2ATaskGetArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            "--history-length" => {
                let value = next_option_value(args, &mut index, "--history-length")?;
                parsed.history_length = Some(parse_u64_option("--history-length", &value)?);
            }
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_task_list_args(args: &[String]) -> Result<A2ATaskListArgs, String> {
    let mut parsed = A2ATaskListArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--context-id" => {
                parsed.context_id = next_option_value(args, &mut index, "--context-id")?;
            }
            "--status" => parsed.status = next_option_value(args, &mut index, "--status")?,
            "--page-size" => {
                let value = next_option_value(args, &mut index, "--page-size")?;
                parsed.page_size = Some(parse_u64_option("--page-size", &value)?);
            }
            "--page-token" => {
                parsed.page_token = next_option_value(args, &mut index, "--page-token")?;
            }
            "--include-artifacts" => {
                parsed.include_artifacts = true;
                index += 1;
            }
            "--output" => parsed.output = next_option_value(args, &mut index, "--output")?,
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_task_cancel_args(args: &[String]) -> Result<A2ATaskCancelArgs, String> {
    let mut parsed = A2ATaskCancelArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_task_subscribe_args(
    args: &[String],
) -> Result<A2ATaskSubscribeArgs, String> {
    let mut parsed = A2ATaskSubscribeArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_push_config_create_args(
    args: &[String],
) -> Result<A2APushConfigCreateArgs, String> {
    let mut parsed = A2APushConfigCreateArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            "--config-id" => {
                parsed.config_id = next_option_value(args, &mut index, "--config-id")?;
            }
            "--callback-url" => {
                parsed.callback_url = next_option_value(args, &mut index, "--callback-url")?;
            }
            "--notification-token" => {
                parsed.notification_token =
                    next_option_value(args, &mut index, "--notification-token")?;
            }
            "--auth-scheme" => {
                parsed.auth_scheme = next_option_value(args, &mut index, "--auth-scheme")?;
            }
            "--auth-credentials" => {
                parsed.auth_credentials =
                    next_option_value(args, &mut index, "--auth-credentials")?;
            }
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_push_config_get_args(
    args: &[String],
) -> Result<A2APushConfigGetArgs, String> {
    let mut parsed = A2APushConfigGetArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            "--config-id" => {
                parsed.config_id = next_option_value(args, &mut index, "--config-id")?;
            }
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_push_config_list_args(
    args: &[String],
) -> Result<A2APushConfigListArgs, String> {
    let mut parsed = A2APushConfigListArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            "--page-size" => {
                let value = next_option_value(args, &mut index, "--page-size")?;
                parsed.page_size = Some(parse_u64_option("--page-size", &value)?);
            }
            "--page-token" => {
                parsed.page_token = next_option_value(args, &mut index, "--page-token")?;
            }
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_push_config_delete_args(
    args: &[String],
) -> Result<A2APushConfigDeleteArgs, String> {
    let mut parsed = A2APushConfigDeleteArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            "--task-id" => parsed.task_id = next_option_value(args, &mut index, "--task-id")?,
            "--config-id" => {
                parsed.config_id = next_option_value(args, &mut index, "--config-id")?;
            }
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn parse_a2a_extended_card_args(args: &[String]) -> Result<A2AExtendedCardArgs, String> {
    let mut parsed = A2AExtendedCardArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--url" => parsed.url = next_option_value(args, &mut index, "--url")?,
            other if parse_a2a_auth_option(other, args, &mut index, &mut parsed.auth)? => {}
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

fn parse_a2a_auth_option(
    option: &str,
    args: &[String],
    index: &mut usize,
    auth: &mut A2AClientAuthArgs,
) -> Result<bool, String> {
    match option {
        "--token" => auth.token = next_option_value(args, index, "--token")?,
        "--basic-username" => {
            auth.basic_username = next_option_value(args, index, "--basic-username")?;
        }
        "--basic-password" => {
            auth.basic_password = next_option_value(args, index, "--basic-password")?;
        }
        "--api-key" => auth.api_key = next_option_value(args, index, "--api-key")?,
        "--api-key-header" => {
            auth.api_key_header = next_option_value(args, index, "--api-key-header")?;
        }
        _ => return Ok(false),
    }
    Ok(true)
}

fn parse_u64_option(option: &str, value: &str) -> Result<u64, String> {
    value
        .parse::<u64>()
        .map_err(|_| format!("Invalid {option} '{}'.", value))
}
