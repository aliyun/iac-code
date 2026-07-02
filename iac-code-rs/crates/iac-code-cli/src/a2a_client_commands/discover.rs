use iac_code_a2a::client::{discover_agent_card, A2ADiscoverOptions};
use iac_code_a2a::transport::A2AAuthConfig;

use crate::a2a_client_args::parse_a2a_discover_args;
use crate::cli_args::non_empty_string;
use crate::json_utils::format_pretty_json;

pub(super) fn run_a2a_client_discover(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_discover_args(args)?;
    if args.url.is_empty() {
        return Err("url is required. Provide --url or url in --config.".to_owned());
    }
    let card = discover_agent_card(A2ADiscoverOptions {
        base_url: &args.url,
        auth: Some(A2AAuthConfig {
            bearer_token: non_empty_string(args.token),
            api_key: non_empty_string(args.api_key),
            api_key_header: args.api_key_header,
            basic_username: non_empty_string(args.basic_username),
            basic_password: non_empty_string(args.basic_password),
        }),
        verification_secret: non_empty_string(args.verify_card_secret),
        verification_jwks_url: non_empty_string(args.verify_card_jwks_url),
        require_card_signature: args.require_card_signature,
        timeout_seconds: None,
    })?;
    Ok(format_pretty_json(&card))
}
