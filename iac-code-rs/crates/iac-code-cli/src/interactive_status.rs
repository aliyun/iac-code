use std::env;

use iac_code_config::cloud_credentials::{
    load_aliyun_credentials_from_iac_code_config, DEFAULT_REGION,
};
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{get_active_provider_key, load_saved_model, DEFAULT_MODEL};
use iac_code_core::{context_window_config, SessionUsageStore, SessionUsageTotals};
use iac_code_tui::terminal_display_width;

use super::ansi::{ANSI_BOLD, ANSI_CYAN, ANSI_DIM, ANSI_RESET};
use super::cli_args::Cli;
use super::cli_i18n::tr;
use super::interactive_usage::format_u64_with_compact_suffix;
use super::session_utils::current_working_directory;

pub(super) fn print_interactive_status(
    cli: &Cli,
    turn_count: u32,
    current_session_id: Option<&str>,
    token_count: u64,
    debug_enabled: bool,
) {
    println!(
        "{}",
        interactive_status_message(
            cli,
            turn_count,
            current_session_id,
            token_count,
            debug_enabled,
        )
    );
}

pub(super) fn interactive_status_message(
    cli: &Cli,
    turn_count: u32,
    current_session_id: Option<&str>,
    token_count: u64,
    debug_enabled: bool,
) -> String {
    let cwd = current_working_directory().unwrap_or_default();
    let (provider, model) = resolve_interactive_status_provider_model(cli);
    let usage =
        current_session_id.and_then(|session_id| load_interactive_session_usage(&cwd, session_id));
    let recorded_usage = usage.as_ref().filter(|usage| usage.has_recorded_usage());
    let mut lines = vec![
        interactive_status_field(&tr("Session"), current_session_id.unwrap_or("interactive")),
        interactive_status_field(&tr("Provider"), &provider),
        interactive_status_field(&tr("Model"), &model),
        interactive_status_field(&tr("Region"), &interactive_status_region()),
        interactive_status_field(&tr("CWD"), &cwd),
        String::new(),
        interactive_status_heading(&tr("API Token Usage (recorded):")),
    ];
    if let Some(usage) = recorded_usage {
        push_interactive_status_usage(&mut lines, usage);
    } else if token_count == 0 {
        lines.push(format!(
            "  {}",
            interactive_status_dim(&tr("No recorded API usage for this session yet."))
        ));
    } else {
        lines.push(interactive_status_field_with_indent(
            &tr("Total"),
            &format_u64_with_commas(token_count),
            2,
        ));
    }
    if debug_enabled {
        lines.push(String::new());
        push_interactive_status_memory_recall(&mut lines);
    }
    lines.push(String::new());
    lines.push(interactive_status_field(
        &tr("Turns"),
        &format!("{turn_count} / {}", cli.max_turns),
    ));
    lines.push(interactive_status_field(
        &tr("Context"),
        &interactive_status_context_usage(&model, recorded_usage, token_count),
    ));
    format_interactive_status_panel(&tr("Session Status"), &lines)
}

pub(super) fn resolve_interactive_status_provider_model(cli: &Cli) -> (String, String) {
    if env::var("IAC_CODE_RS_FAKE_PROVIDER").ok().as_deref() == Some("1") {
        return ("fake".to_owned(), "fake".to_owned());
    }
    let paths = match ConfigPaths::from_env() {
        Ok(paths) => paths,
        Err(_) => return ("not configured".to_owned(), cli_model_or_default(cli)),
    };
    let provider = get_active_provider_key(&paths)
        .ok()
        .flatten()
        .unwrap_or_else(|| "not configured".to_owned());
    let model = if cli.model.trim().is_empty() {
        load_saved_model(&paths)
            .ok()
            .flatten()
            .unwrap_or_else(|| DEFAULT_MODEL.to_owned())
    } else {
        cli.model.trim().to_owned()
    };
    (provider, model)
}

fn cli_model_or_default(cli: &Cli) -> String {
    if cli.model.trim().is_empty() {
        DEFAULT_MODEL.to_owned()
    } else {
        cli.model.trim().to_owned()
    }
}

fn load_interactive_session_usage(cwd: &str, session_id: &str) -> Option<SessionUsageTotals> {
    let paths = ConfigPaths::from_env().ok()?;
    Some(SessionUsageStore::new(paths.subdirs().projects).load(cwd, session_id))
}

fn push_interactive_status_usage(lines: &mut Vec<String>, usage: &SessionUsageTotals) {
    lines.push(interactive_status_field_with_indent(
        &tr("Input"),
        &format_u64_with_commas(usage.input_tokens),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Output"),
        &format_u64_with_commas(usage.output_tokens),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Cache read"),
        &format_u64_with_commas(usage.cache_read_input_tokens),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Total"),
        &format_u64_with_commas(usage.total_tokens()),
        2,
    ));
}

fn push_interactive_status_memory_recall(lines: &mut Vec<String>) {
    lines.push(interactive_status_heading(&tr("Memory Recall")));
    lines.push(interactive_status_field_with_indent(
        &tr("Side queries"),
        &tr("{total} total, {success} success, {failed} failed, {cancelled} cancelled")
            .replace("{total}", "0")
            .replace("{success}", "0")
            .replace("{failed}", "0")
            .replace("{cancelled}", "0"),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Last attempt"),
        &tr("{status} in {duration} ms, {count} files selected")
            .replace("{status}", "skipped")
            .replace("{duration}", "0")
            .replace("{count}", "0"),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Side call usage"),
        &tr("No token usage reported"),
        2,
    ));
    lines.push(interactive_status_field_with_indent(
        &tr("Last usage"),
        &tr("No token usage reported"),
        2,
    ));
}

fn interactive_status_field_with_indent(label: &str, value: &str, indent: usize) -> String {
    format!(
        "{}{}",
        " ".repeat(indent),
        interactive_status_field(label, value)
    )
}

fn interactive_status_field(label: &str, value: &str) -> String {
    const LABEL_WIDTH: usize = 14;
    let label = format!("{label}:");
    let padding = LABEL_WIDTH
        .saturating_sub(terminal_display_width(&label))
        .max(1);
    format!(
        "{ANSI_BOLD}{label}{ANSI_RESET}{}{value}",
        " ".repeat(padding)
    )
}

fn interactive_status_heading(text: &str) -> String {
    format!("{ANSI_BOLD}{text}{ANSI_RESET}")
}

fn interactive_status_dim(text: &str) -> String {
    format!("{ANSI_DIM}{text}{ANSI_RESET}")
}

fn interactive_status_region() -> String {
    ConfigPaths::from_env()
        .ok()
        .and_then(|paths| {
            load_aliyun_credentials_from_iac_code_config(&paths.cloud_credentials_path).ok()
        })
        .flatten()
        .map(|credential| credential.region_id)
        .filter(|region| !region.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_REGION.to_owned())
}

fn interactive_status_context_usage(
    model: &str,
    usage: Option<&SessionUsageTotals>,
    token_count: u64,
) -> String {
    let used = usage
        .filter(|usage| usage.has_recorded_usage())
        .map(SessionUsageTotals::total_tokens)
        .unwrap_or(token_count);
    let context_window = context_window_config(model).context_window;
    if context_window == 0 {
        return tr("not reported");
    }
    let percent = used as f64 / context_window as f64 * 100.0;
    tr("used {percent}% ({used} / {total})")
        .replace("{percent}", &format!("{percent:.0}"))
        .replace("{used}", &format_u64_with_compact_suffix(used))
        .replace("{total}", &format_u64_with_compact_suffix(context_window))
}

fn format_interactive_status_panel(title: &str, lines: &[String]) -> String {
    let content_width = lines
        .iter()
        .map(|line| terminal_display_width_without_ansi(line))
        .max()
        .unwrap_or(0)
        .max(terminal_display_width(title) + 2);
    let title_segment = format!(" {title} ");
    let inner_width = content_width + 2;
    let title_width = terminal_display_width(&title_segment);
    let left = (inner_width - title_width) / 2;
    let right = inner_width - title_width - left;
    let mut output = format!(
        "{ANSI_CYAN}╭{}{}{}╮{ANSI_RESET}",
        "─".repeat(left),
        title_segment,
        "─".repeat(right)
    );
    for line in lines {
        output.push('\n');
        output.push_str(&format!("{ANSI_CYAN}│{ANSI_RESET} "));
        output.push_str(line);
        let padding = content_width.saturating_sub(terminal_display_width_without_ansi(line));
        output.push_str(&" ".repeat(padding));
        output.push_str(&format!(" {ANSI_CYAN}│{ANSI_RESET}"));
    }
    output.push('\n');
    output.push_str(&format!(
        "{ANSI_CYAN}╰{}╯{ANSI_RESET}",
        "─".repeat(content_width + 2)
    ));
    output
}

fn terminal_display_width_without_ansi(text: &str) -> usize {
    terminal_display_width(&strip_ansi_sequences(text))
}

fn strip_ansi_sequences(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for sequence_ch in chars.by_ref() {
                if ('@'..='~').contains(&sequence_ch) {
                    break;
                }
            }
            continue;
        }
        output.push(ch);
    }
    output
}

fn format_u64_with_commas(value: u64) -> String {
    let digits = value.to_string();
    let mut output = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, character) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            output.push(',');
        }
        output.push(character);
    }
    output
}
