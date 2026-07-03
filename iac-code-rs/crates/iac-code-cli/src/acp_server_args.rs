use super::acp_http::run_acp_http_server;
use super::acp_stdio::run_acp_stdio_server;
use crate::cli_args::next_option_value;

#[derive(Clone, Debug)]
pub(super) struct AcpServerArgs {
    pub(super) host: String,
    pub(super) port: u16,
    pub(super) transport: String,
    pub(super) debug: bool,
}

impl Default for AcpServerArgs {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_owned(),
            port: 8765,
            transport: "stdio".to_owned(),
            debug: false,
        }
    }
}

pub(super) fn parse_acp_server_args(args: &[String]) -> Result<AcpServerArgs, String> {
    let mut parsed = AcpServerArgs::default();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
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
            "--debug" | "-d" => {
                parsed.debug = true;
                index += 1;
            }
            other => return Err(format!("No such option: {other}")),
        }
    }
    Ok(parsed)
}

pub(super) fn handle_acp_server_command(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("acp") {
        return None;
    }
    match run_acp_server(&args[1..]) {
        Ok(()) => Some(0),
        Err(error) => {
            eprintln!("{error}");
            Some(1)
        }
    }
}

fn run_acp_server(args: &[String]) -> Result<(), String> {
    let args = parse_acp_server_args(args)?;
    match args.transport.as_str() {
        "stdio" => run_acp_stdio_server(args),
        "http" => run_acp_http_server(args),
        other => Err(format!(
            "Unsupported ACP transport '{other}'. Supported values: stdio, http"
        )),
    }
}
