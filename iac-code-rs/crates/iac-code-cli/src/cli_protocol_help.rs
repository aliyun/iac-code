use iac_code_exec::EXIT_OK;

use crate::cli_a2a_client_help::handle_a2a_client_help;

use super::cli_i18n::tr;

pub(super) fn handle_protocol_command_help(args: &[String]) -> bool {
    match args.first().map(String::as_str) {
        Some("update") if command_help_requested(args, 1) => {
            print_update_help();
            true
        }
        Some("acp") if command_help_requested(args, 1) => {
            print_acp_help();
            true
        }
        Some("a2a") if args.iter().skip(1).any(|arg| is_help_flag(arg)) => {
            print_a2a_help();
            true
        }
        Some("a2a-client") => handle_a2a_client_help(args),
        _ => false,
    }
}

pub(super) fn handle_update_command(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("update") {
        return None;
    }
    for arg in &args[1..] {
        match arg.as_str() {
            "--check" => {}
            "--help" | "-h" => {
                print_update_help();
                return Some(EXIT_OK);
            }
            option if option.starts_with('-') => {
                eprintln!(
                    "Usage: iac-code update [OPTIONS]\nTry 'iac-code update -h' for help.\nError: No such option: {option}"
                );
                return Some(2);
            }
            value => {
                eprintln!(
                    "Usage: iac-code update [OPTIONS]\nTry 'iac-code update -h' for help.\nError: Got unexpected extra argument ({value})"
                );
                return Some(2);
            }
        }
    }
    eprintln!(
        "iac-code self-update is not available in Rust local builds. Build the Rust binary locally with `cargo build --release -p iac-code-cli`."
    );
    Some(1)
}

fn command_help_requested(args: &[String], command_parts: usize) -> bool {
    args.len() == command_parts + 1 && is_help_flag(&args[command_parts])
}

fn is_help_flag(value: &str) -> bool {
    matches!(value, "--help" | "-h")
}

fn print_update_help() {
    println!(
        "Usage: iac-code update [OPTIONS]\n\
\n\
{}\n\
\n\
{}:\n\
      --check  {}\n\
  -h, --help   {}",
        tr("Update iac-code to the latest available version."),
        tr("Options"),
        tr("Check for updates without installing."),
        tr("Show this message and exit.")
    );
}

fn print_acp_help() {
    println!(
        "Usage: iac-code acp [OPTIONS]\n\
\n\
Run iac-code as an ACP server.\n\
\n\
Options:\n\
      --transport <TEXT>  Transport type: stdio or http [default: stdio]\n\
      --port <INTEGER>   HTTP server port [default: 8765]\n\
      --host <TEXT>      HTTP server host [default: 127.0.0.1]\n\
  -d, --debug            Enable debug logging\n\
  -h, --help             Show this message and exit"
    );
}

fn print_a2a_help() {
    println!(
        "Usage: iac-code a2a [OPTIONS]\n\
\n\
Run iac-code as an A2A 1.0 server.\n\
\n\
Options:\n\
      --config <TEXT>             YAML config file for A2A server options\n\
      --host <TEXT>               HTTP server host [default: 127.0.0.1]\n\
      --port <INTEGER>            HTTP server port [default: 41242]\n\
      --transport <TEXT>          A2A transport: http, stdio, unix, websocket, grpc, grpc-jsonrpc, or redis-streams\n\
      --thinking-exposure <TEXT>  Expose A2A thinking signal types; repeat for multiple\n\
      --log-to-stdout             Mirror server logs to stdout\n\
  -d, --debug                     Enable debug logging\n\
  -h, --help                      Show this message and exit"
    );
}
