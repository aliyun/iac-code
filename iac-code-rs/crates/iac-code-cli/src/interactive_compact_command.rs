use iac_code_config::paths::ConfigPaths;
use iac_code_core::{context_window_config, AgentLoop, SessionStorage};
use iac_code_exec::OutputFormat;
use iac_code_protocol::message::Conversation;

use crate::cli_args::Cli;
use crate::cli_i18n::{tr, tr_compaction_result, tr_compaction_too_small, tr_turns};
use crate::interactive_commands::print_interactive_command_result;
use crate::interactive_working::start_interactive_working_indicator_with_status;
use crate::provider_config::load_configured_provider;
use crate::session_utils::{current_git_branch, current_working_directory};

pub(super) fn print_interactive_compact(
    cli: &Cli,
    current_session_id: Option<&str>,
    output_format: OutputFormat,
) {
    let mut working_indicator = start_interactive_working_indicator_with_status(
        output_format,
        interactive_compact_status(),
    );
    let message = match interactive_compact_message(cli, current_session_id) {
        Ok(message) => message,
        Err(error) => error,
    };
    if let Some(indicator) = working_indicator.as_mut() {
        indicator.stop_and_clear();
    }
    print_interactive_command_result(&message);
}

fn interactive_compact_message(
    cli: &Cli,
    current_session_id: Option<&str>,
) -> Result<String, String> {
    let Some(session_id) = current_session_id else {
        return Ok(tr("Nothing to compact: conversation is empty."));
    };
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let storage =
        SessionStorage::new(paths.subdirs().projects).map_err(|error| error.to_string())?;
    let messages = storage
        .load(&cwd, session_id)
        .map_err(|error| error.to_string())?;
    let (provider, model) = load_configured_provider(&paths, &cli.model)?;
    let mut agent_loop = AgentLoop::new(provider, 1);
    agent_loop.set_model(model.clone());
    agent_loop.set_conversation(Conversation { messages });
    let usage_before_compaction = agent_loop.context_usage();
    let context_config = context_window_config(&model);
    let result = agent_loop.compact();

    match result.status.as_str() {
        "empty" => Ok(tr("Nothing to compact: conversation is empty.")),
        "too_short" => Ok(tr_turns(
            "Conversation too short to compact: all messages are within the recent {turns}-turn preservation window.",
            result.preserve_recent_turns,
        )),
        "too_small" => Ok(tr_compaction_too_small(
            usage_before_compaction.total_tokens,
            context_config.compact_buffer,
        )),
        "failed" => Ok(tr("Compaction failed. See logs for details.")),
        "success" => {
            let git_branch = current_git_branch(&cwd);
            storage
                .save(
                    &cwd,
                    session_id,
                    &agent_loop.conversation().messages,
                    git_branch.as_deref(),
                )
                .map_err(|error| error.to_string())?;
            let usage_percent =
                result.compacted_tokens as f64 / context_config.context_window as f64 * 100.0;
            Ok(tr_compaction_result(
                result.original_tokens,
                result.compacted_tokens,
                &format!("{usage_percent:.0}%"),
            ))
        }
        _ => Ok(tr("Compaction failed. See logs for details.")),
    }
}

pub(super) fn interactive_compact_status() -> String {
    tr("Compacting context")
}
