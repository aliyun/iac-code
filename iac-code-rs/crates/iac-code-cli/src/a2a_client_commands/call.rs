use iac_code_a2a::client::{call_agent, stream_agent, A2ACallOptions};
use iac_code_a2a::transport::A2AAuthConfig;

use crate::a2a_client_args::parse_a2a_call_args;
use crate::a2a_client_format::format_a2a_stream_event;
use crate::a2a_client_routes::resolve_a2a_call_url;
use crate::cli_args::{non_empty_str, non_empty_string};
use crate::json_utils::format_pretty_json;
use crate::session_utils::current_working_directory;

pub(super) fn run_a2a_client_call(args: &[String]) -> Result<String, String> {
    let args = parse_a2a_call_args(args)?;
    if args.prompt.is_empty() {
        return Err("Missing option '--prompt' / '-p'.".to_owned());
    }

    let url = resolve_a2a_call_url(&args)?;
    let cwd = if args.cwd.is_empty() || args.cwd == "." {
        current_working_directory()?
    } else {
        args.cwd.clone()
    };
    let options = A2ACallOptions {
        base_url: &url,
        prompt: &args.prompt,
        cwd: &cwd,
        context_id: non_empty_str(&args.context_id),
        model: non_empty_str(&args.model),
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
        timeout_seconds: Some(args.timeout_seconds),
    };
    if args.stream {
        let events = stream_agent(options)?;
        return Ok(events
            .iter()
            .map(format_a2a_stream_event)
            .collect::<Vec<_>>()
            .join("\n"));
    }
    let response = call_agent(options)?;
    let text = response.text();
    if text.is_empty() {
        Ok(format_pretty_json(&response.payload))
    } else {
        Ok(text)
    }
}
