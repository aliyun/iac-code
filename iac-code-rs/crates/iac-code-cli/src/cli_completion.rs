use std::env;
use std::path::Path;

use iac_code_exec::EXIT_OK;

const COMPLETION_PROG_NAME: &str = "iac-code";
const COMPLETION_VAR: &str = "_IAC_CODE_COMPLETE";

const TOP_LEVEL_COMMAND_COMPLETIONS: &[(&str, &str)] = &[
    ("update", "Update iac-code to the latest version."),
    ("acp", "Run iac-code as an ACP server."),
    ("a2a", "Run iac-code as an A2A 1.0 server."),
    ("a2a-client", "Use iac-code as an A2A client."),
];

const TOP_LEVEL_OPTION_COMPLETIONS: &[(&str, &str)] = &[
    ("--model", "LLM model to use"),
    (
        "--prompt",
        "Non-interactive mode: run a single prompt and exit",
    ),
    ("--output-format", "Output format: text, json, stream-json"),
    ("--max-turns", "Maximum agent turns in headless mode"),
    ("--debug", "Enable debug logging"),
    ("--verbose", "Show headless progress on stderr"),
    ("--version", "Show version and exit"),
    ("--resume", "Resume a session by ID or name"),
    ("--continue", "Resume the most recent session"),
    (
        "--install-completion",
        "Install completion for the current shell.",
    ),
    (
        "--show-completion",
        "Show completion for the current shell, to copy it or customize the installation.",
    ),
    (
        "--allowed-tools",
        "Comma-separated tool permission patterns to allow, e.g. 'bash(git *),write_file'",
    ),
    (
        "--disallowed-tools",
        "Comma-separated tool permission patterns to deny",
    ),
    (
        "--permission-mode",
        "Permission mode: default, accept_edits, bypass_permissions, dont_ask",
    ),
    ("--help", "Show this message and exit."),
];

pub(super) fn handle_shell_completion_protocol() -> Option<i32> {
    let instruction = env::var(COMPLETION_VAR).ok()?;
    match instruction.as_str() {
        "complete_zsh" => {
            print_zsh_completion_matches();
            Some(EXIT_OK)
        }
        "complete_bash" => {
            print_bash_completion_matches();
            Some(EXIT_OK)
        }
        "complete_fish" => {
            print_fish_completion_matches();
            Some(EXIT_OK)
        }
        unsupported => {
            let shell = unsupported
                .rsplit_once('_')
                .map_or(unsupported, |(_, shell)| shell);
            eprintln!("Shell {shell} not supported.");
            Some(1)
        }
    }
}

pub(super) fn handle_completion_command(args: &[String]) -> Option<i32> {
    if args.iter().any(|arg| arg == "--show-completion") {
        return Some(match detected_completion_shell().as_deref() {
            Some("zsh") => {
                print_zsh_completion_script();
                EXIT_OK
            }
            Some("bash") => {
                print_bash_completion_script();
                EXIT_OK
            }
            Some("fish") => {
                print_fish_completion_script();
                EXIT_OK
            }
            Some(shell) => {
                eprintln!("Shell {shell} not supported.");
                1
            }
            None => {
                eprintln!("Shell  not supported.");
                1
            }
        });
    }
    if args.iter().any(|arg| arg == "--install-completion") {
        eprintln!(
            "iac-code completion installation is not available in Rust local builds. Use --show-completion and install the script manually."
        );
        return Some(1);
    }
    None
}

fn detected_completion_shell() -> Option<String> {
    let shell = env::var("SHELL").ok()?;
    let name = Path::new(&shell).file_name()?.to_string_lossy();
    if name.contains("zsh") {
        Some("zsh".to_owned())
    } else if name.contains("bash") {
        Some("bash".to_owned())
    } else if name.contains("fish") {
        Some("fish".to_owned())
    } else {
        Some(name.into_owned())
    }
}

fn print_zsh_completion_script() {
    println!(
        "#compdef {COMPLETION_PROG_NAME}\n\
\n\
_iac_code_completion() {{\n\
  eval $(env _TYPER_COMPLETE_ARGS=\"${{words[1,$CURRENT]}}\" {COMPLETION_VAR}=complete_zsh {COMPLETION_PROG_NAME})\n\
}}\n\
\n\
compdef _iac_code_completion {COMPLETION_PROG_NAME}"
    );
}

fn print_bash_completion_script() {
    println!(
        "_iac_code_completion() {{\n\
    local IFS=$'\\n'\n\
    COMPREPLY=( $( env COMP_WORDS=\"${{COMP_WORDS[*]}}\" \\\n\
                   COMP_CWORD=$COMP_CWORD \\\n\
                   {COMPLETION_VAR}=complete_bash $1 ) )\n\
    return 0\n\
}}\n\
\n\
complete -o default -F _iac_code_completion {COMPLETION_PROG_NAME}"
    );
}

fn print_fish_completion_script() {
    println!(
        "complete --command {COMPLETION_PROG_NAME} --no-files --arguments \"(env {COMPLETION_VAR}=complete_fish _TYPER_COMPLETE_FISH_ACTION=get-args _TYPER_COMPLETE_ARGS=(commandline -cp) {COMPLETION_PROG_NAME})\" --condition \"env {COMPLETION_VAR}=complete_fish _TYPER_COMPLETE_FISH_ACTION=is-args _TYPER_COMPLETE_ARGS=(commandline -cp) {COMPLETION_PROG_NAME}\""
    );
}

fn print_zsh_completion_matches() {
    let args = env::var("_TYPER_COMPLETE_ARGS").unwrap_or_default();
    let prefix = completion_prefix_from_words(&args);
    let items = top_level_completion_items(&prefix);
    println!("_arguments '*: :({})'", zsh_completion_items(&items));
}

fn print_bash_completion_matches() {
    let words = env::var("COMP_WORDS").unwrap_or_default();
    let cword = env::var("COMP_CWORD")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0);
    let split_words = words.split_whitespace().collect::<Vec<_>>();
    let prefix = split_words.get(cword).copied().unwrap_or_default();
    for (value, _) in top_level_completion_items(prefix) {
        println!("{value}");
    }
}

fn print_fish_completion_matches() {
    if env::var("_TYPER_COMPLETE_FISH_ACTION").ok().as_deref() == Some("is-args") {
        return;
    }
    let args = env::var("_TYPER_COMPLETE_ARGS").unwrap_or_default();
    let prefix = completion_prefix_from_words(&args);
    for (value, description) in top_level_completion_items(&prefix) {
        println!("{value}\t{description}");
    }
}

fn completion_prefix_from_words(words: &str) -> String {
    if words.ends_with(' ') {
        return String::new();
    }
    words
        .split_whitespace()
        .last()
        .map(str::to_owned)
        .unwrap_or_default()
}

fn top_level_completion_items(prefix: &str) -> Vec<(&'static str, &'static str)> {
    let candidates = if prefix.starts_with('-') {
        TOP_LEVEL_OPTION_COMPLETIONS
    } else {
        TOP_LEVEL_COMMAND_COMPLETIONS
    };
    candidates
        .iter()
        .copied()
        .filter(|(value, _)| value.starts_with(prefix))
        .collect()
}

fn zsh_completion_items(items: &[(&str, &str)]) -> String {
    items
        .iter()
        .map(|(value, description)| {
            format!(
                "\"{}\":\"{}\"",
                escape_zsh_completion_value(value),
                escape_zsh_completion_value(description)
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn escape_zsh_completion_value(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace(':', "\\:")
}
