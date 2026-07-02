use super::A2AClientConfig;

pub(super) fn append_route_config_option(args: &mut Vec<String>, config: &A2AClientConfig) {
    if option_present(args, "--route") {
        return;
    }
    for value in config_values_for_first_key(config, &["route", "routes"]) {
        args.extend(["--route".to_owned(), value.to_owned()]);
    }
}

pub(super) fn append_a2a_auth_config_options(args: &mut Vec<String>, config: &A2AClientConfig) {
    append_config_option(args, config, "token", "--token");
    append_config_option(args, config, "basic_username", "--basic-username");
    append_config_option(args, config, "basic_password", "--basic-password");
    append_config_option(args, config, "api_key", "--api-key");
    append_config_option(args, config, "api_key_header", "--api-key-header");
}

pub(super) fn append_a2a_card_config_options(args: &mut Vec<String>, config: &A2AClientConfig) {
    append_config_option_with_aliases(
        args,
        config,
        "verify_card_secret",
        "--verify-card-secret",
        &["--verify-card-secret", "--signing-secret"],
    );
    append_config_option(
        args,
        config,
        "verify_card_jwks_url",
        "--verify-card-jwks-url",
    );
    append_config_bool_flag_with_aliases(
        args,
        config,
        "require_card_signature",
        "--require-card-signature",
        &["--require-card-signature", "--require-signature"],
    );
}

pub(super) fn append_config_option(
    args: &mut Vec<String>,
    config: &A2AClientConfig,
    key: &str,
    option: &str,
) {
    append_config_option_with_aliases(args, config, key, option, &[option]);
}

fn append_config_option_with_aliases(
    args: &mut Vec<String>,
    config: &A2AClientConfig,
    key: &str,
    option: &str,
    aliases: &[&str],
) {
    if option_present_any(args, aliases) {
        return;
    }
    if let Some(value) = config_values(config, key).next() {
        args.extend([option.to_owned(), value.to_owned()]);
    }
}

pub(super) fn append_config_option_for_first_key(
    args: &mut Vec<String>,
    config: &A2AClientConfig,
    keys: &[&str],
    option: &str,
    aliases: &[&str],
) {
    if option_present_any(args, aliases) {
        return;
    }
    if let Some(value) = config_values_for_first_key(config, keys).first() {
        args.extend([option.to_owned(), (*value).to_owned()]);
    }
}

pub(super) fn append_config_bool_flag(
    args: &mut Vec<String>,
    config: &A2AClientConfig,
    key: &str,
    option: &str,
) {
    append_config_bool_flag_with_aliases(args, config, key, option, &[option]);
}

fn append_config_bool_flag_with_aliases(
    args: &mut Vec<String>,
    config: &A2AClientConfig,
    key: &str,
    option: &str,
    aliases: &[&str],
) {
    if option_present_any(args, aliases) {
        return;
    }
    if config.get(key).is_some_and(|value| yaml_bool_value(value)) {
        args.push(option.to_owned());
    }
}

fn option_present(args: &[String], option: &str) -> bool {
    option_present_any(args, &[option])
}

fn option_present_any(args: &[String], options: &[&str]) -> bool {
    args.iter()
        .any(|arg| options.iter().any(|option| arg == option))
}

pub(super) fn yaml_bool_value(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "true" | "1" | "yes" | "on"
    )
}

pub(super) fn config_values<'a>(
    config: &'a A2AClientConfig,
    key: &str,
) -> impl Iterator<Item = &'a str> + 'a {
    config
        .get(key)
        .into_iter()
        .flat_map(|value| value.lines())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn config_values_for_first_key<'a>(config: &'a A2AClientConfig, keys: &[&str]) -> Vec<&'a str> {
    for key in keys {
        let values = config_values(config, key).collect::<Vec<_>>();
        if !values.is_empty() {
            return values;
        }
    }
    Vec::new()
}

pub(super) fn apply_config_string(target: &mut String, config: &A2AClientConfig, key: &str) {
    if let Some(value) = config.get(key).filter(|value| !value.is_empty()) {
        *target = value.clone();
    }
}

pub(super) fn apply_config_u16(target: &mut u16, config: &A2AClientConfig, key: &str) {
    if let Some(value) = config.get(key).and_then(|value| value.parse::<u16>().ok()) {
        *target = value;
    }
}

pub(super) fn apply_config_u64(target: &mut u64, config: &A2AClientConfig, key: &str) {
    if let Some(value) = config.get(key).and_then(|value| value.parse::<u64>().ok()) {
        *target = value;
    }
}

pub(super) fn apply_config_optional_u16(
    target: &mut Option<u16>,
    config: &A2AClientConfig,
    key: &str,
) {
    if let Some(value) = config.get(key).and_then(|value| value.parse::<u16>().ok()) {
        *target = Some(value);
    }
}
