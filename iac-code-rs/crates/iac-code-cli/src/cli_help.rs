use super::cli_i18n::{tr, tr_default};

pub(super) fn print_help() {
    println!(
        "Usage: iac-code [OPTIONS] COMMAND [ARGS]...\n\
\n\
{}\n\
\n\
{}:\n\
  -m, --model <TEXT>              {}\n\
  -p, --prompt <TEXT>             {}\n\
      --output-format <TEXT>      {} [{}]\n\
      --max-turns <INTEGER>       {} [{}]\n\
  -d, --debug                     {}\n\
      --verbose                   {}\n\
  -v, -V, --version               {}\n\
  -r, --resume <TEXT>             {}\n\
  -c, --continue                  {}\n\
      --install-completion        {}\n\
      --show-completion           {}\n\
      --allowed-tools <TEXT>      {}\n\
      --disallowed-tools <TEXT>   {}\n\
      --permission-mode <TEXT>    {}\n\
  -h, --help                      {}\n\
\n\
{}:\n\
  update      {}\n\
  acp         {}\n\
  a2a         {}\n\
  a2a-client  {}",
        tr("AI-powered infrastructure orchestration tool"),
        tr("Options"),
        tr("LLM model to use"),
        tr("Non-interactive mode: run a single prompt and exit"),
        tr("Output format: text, json, stream-json"),
        tr_default("text"),
        tr("Maximum agent turns in headless mode"),
        tr_default("100"),
        tr("Enable debug logging"),
        tr("Show headless progress on stderr"),
        tr("Show version and exit"),
        tr("Resume a session by ID or name"),
        tr("Resume the most recent session"),
        tr("Install completion for the current shell."),
        tr("Show completion for the current shell, to copy it or customize the installation."),
        tr("Comma-separated tool permission patterns to allow, e.g. 'bash(git *),write_file'"),
        tr("Comma-separated tool permission patterns to deny"),
        tr("Permission mode: default, accept_edits, bypass_permissions, dont_ask"),
        tr("Show this message and exit."),
        tr("Commands"),
        tr("Update iac-code to the latest version."),
        tr("Run iac-code as an ACP server."),
        tr("Run iac-code as an A2A 1.0 server."),
        tr("Use iac-code as an A2A client.")
    );
}

pub(super) fn handle_unknown_top_level_command(args: &[String]) -> Option<i32> {
    let command = args.first()?;
    if command.starts_with('-') {
        return None;
    }
    if matches!(command.as_str(), "update" | "acp" | "a2a" | "a2a-client") {
        return None;
    }
    eprintln!("{}", no_such_command_message("iac-code", command));
    Some(2)
}

pub(super) fn no_such_command_message(command_path: &str, command: &str) -> String {
    format!(
        "Usage: {command_path} [OPTIONS] COMMAND [ARGS]...\nTry '{command_path} -h' for help.\nError: No such command '{command}'."
    )
}
