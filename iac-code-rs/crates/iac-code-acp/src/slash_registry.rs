mod debug_log;
mod locale;
mod memory;

use crate::session::{AcpAgent, CompactStatus, RenameOutcome};
use debug_log::{current_debug_log_path, disable_acp_debug_log, enable_acp_debug_log};
use locale::{
    tr, tr_acp_compaction_result, tr_acp_unsupported_command, tr_known_agent_error, tr_name,
    tr_turns, tr_value,
};
use memory::{format_memory_detail, format_memory_summary};

pub const ACP_SUPPORTED_COMMANDS: &[&str] = &["clear", "compact", "debug", "memory", "rename"];
const MEMORY_USAGE: &str = "Usage: /memory [<name>|search <query>|delete <name>|help]";

#[derive(Clone, Debug, Default)]
pub struct AcpSlashRegistry;

impl AcpSlashRegistry {
    pub fn is_slash_command(&self, text: &str) -> bool {
        let stripped = text.trim();
        stripped.starts_with('/') && stripped.len() > 1 && !stripped.starts_with("//")
    }

    pub fn execute<A>(&self, text: &str, agent: &mut A) -> String
    where
        A: AcpAgent,
    {
        let stripped = text.trim();
        let mut parts = stripped[1..].splitn(2, char::is_whitespace);
        let command = parts.next().unwrap_or_default().to_ascii_lowercase();
        let args = parts.next().unwrap_or_default().trim();

        if !ACP_SUPPORTED_COMMANDS.contains(&command.as_str()) {
            let supported = ACP_SUPPORTED_COMMANDS
                .iter()
                .map(|command| format!("/{command}"))
                .collect::<Vec<_>>()
                .join(", ");
            return tr_acp_unsupported_command(&command, &supported);
        }

        match command.as_str() {
            "compact" => self.handle_compact(agent),
            "clear" => self.handle_clear(agent),
            "debug" => self.handle_debug(args),
            "memory" => self.handle_memory(args, agent),
            "rename" => self.handle_rename(args, agent),
            _ => format!("Command '/{command}' handler not implemented."),
        }
    }

    fn handle_compact<A>(&self, agent: &mut A) -> String
    where
        A: AcpAgent,
    {
        let result = match agent.compact() {
            Ok(result) => result,
            Err(error) => return tr_value("Compaction failed: {error}", "error", &error),
        };

        match result.status {
            CompactStatus::Empty => tr("Nothing to compact: conversation is empty."),
            CompactStatus::TooShort => tr_turns(
                "Conversation too short to compact: all messages are within the recent {turns}-turn preservation window.",
                result.preserve_recent_turns,
            ),
            CompactStatus::TooSmall => tr(
                "Conversation too small to compact: current context is below the compaction threshold.",
            ),
            CompactStatus::Failed => tr("Compaction failed. See logs for details."),
            CompactStatus::Success => tr_acp_compaction_result(
                result.original_tokens,
                result.compacted_tokens,
                &format!("{:.0}%", agent.context_usage_percent()),
            ),
        }
    }

    fn handle_clear<A>(&self, agent: &mut A) -> String
    where
        A: AcpAgent,
    {
        match agent.reset() {
            Ok(()) => tr("Conversation history cleared."),
            Err(error) => tr_value("Clear failed: {error}", "error", &error),
        }
    }

    fn handle_debug(&self, args: &str) -> String {
        match args.trim().to_ascii_lowercase().as_str() {
            "" | "status" => match current_debug_log_path() {
                Some(path) => tr_value(
                    "Debug logging is on. Log file: {path}",
                    "path",
                    &path.display().to_string(),
                ),
                None => tr("Debug logging is off."),
            },
            "on" => match enable_acp_debug_log() {
                Ok(path) => tr_value(
                    "Debug logging enabled. Log file: {path}",
                    "path",
                    &path.display().to_string(),
                ),
                Err(error) => format!("Debug logging failed: {error}"),
            },
            "off" => {
                disable_acp_debug_log();
                tr("Debug logging disabled.")
            }
            _ => tr("Usage: /debug [on|off]"),
        }
    }

    fn handle_memory<A>(&self, args: &str, agent: &mut A) -> String
    where
        A: AcpAgent,
    {
        let args = args.split_whitespace().collect::<Vec<_>>();
        if args.is_empty() {
            return match agent.memory_entries() {
                Some(memories) => format_memory_summary(&tr("Saved memories:"), memories)
                    .unwrap_or_else(|| tr("No memories saved yet.")),
                None => tr("Memory manager is unavailable."),
            };
        }

        match args[0].to_ascii_lowercase().as_str() {
            "help" => tr(MEMORY_USAGE),
            "search" => {
                let query = args[1..].join(" ");
                if query.trim().is_empty() {
                    return tr(MEMORY_USAGE);
                }
                match agent.search_memories(&query) {
                    Ok(memories) => format_memory_summary(&tr("Matching memories:"), memories)
                        .unwrap_or_else(|| tr("No matching memories.")),
                    Err(error) => tr_known_agent_error(&error),
                }
            }
            "delete" => {
                if args.len() != 2 {
                    return tr(MEMORY_USAGE);
                }
                let name = args[1];
                match agent.load_memory(name) {
                    Ok(Some(_)) => match agent.delete_memory(name) {
                        Ok(true) => tr_name("Memory '{name}' deleted.", name),
                        Ok(false) => tr_name("Memory '{name}' not found.", name),
                        Err(error) => tr_known_agent_error(&error),
                    },
                    Ok(None) => tr_name("Memory '{name}' not found.", name),
                    Err(error) => tr_known_agent_error(&error),
                }
            }
            _ => {
                if args.len() != 1 {
                    return tr(MEMORY_USAGE);
                }
                let name = args[0];
                match agent.load_memory(name) {
                    Ok(Some(memory)) => format_memory_detail(memory),
                    Ok(None) => tr_name("Memory '{name}' not found.", name),
                    Err(error) => tr_known_agent_error(&error),
                }
            }
        }
    }

    fn handle_rename<A>(&self, args: &str, agent: &mut A) -> String
    where
        A: AcpAgent,
    {
        let parts = args.split_whitespace().collect::<Vec<_>>();
        if parts.len() != 1 {
            return tr("Usage: /rename <name>");
        }

        match agent.rename_session(parts[0]) {
            Ok(RenameOutcome::Unchanged) => tr_name("Session is already named {name}", parts[0]),
            Ok(RenameOutcome::Renamed) => tr_name("Renamed session to {name}", parts[0]),
            Err(error) => tr_known_agent_error(&error),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::{CompactResult, PermissionDecision};
    use iac_code_protocol::{PermissionRequestEvent, StreamEvent};

    struct CompactAgent {
        result: CompactResult,
    }

    impl AcpAgent for CompactAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }

        fn compact(&mut self) -> Result<CompactResult, String> {
            Ok(self.result.clone())
        }
    }

    #[test]
    fn compact_success_reports_increase_direction() {
        let registry = AcpSlashRegistry;
        let mut agent = CompactAgent {
            result: CompactResult {
                status: CompactStatus::Success,
                original_tokens: 209,
                compacted_tokens: 267,
                preserve_recent_turns: 3,
            },
        };

        let message = registry.execute("/compact", &mut agent);

        assert!(message.contains("209"));
        assert!(message.contains("267"));
        assert!(message.contains("28%"));
        assert!(
            message.contains("increase") || message.contains("增加"),
            "{message}"
        );
        assert!(!message.contains("-28%"));
        assert!(!message.contains("reduction"));
        assert!(!message.contains("减少"));
    }

    #[test]
    fn compact_too_small_reports_threshold_status() {
        let registry = AcpSlashRegistry;
        let mut agent = CompactAgent {
            result: CompactResult {
                status: CompactStatus::TooSmall,
                original_tokens: 0,
                compacted_tokens: 0,
                preserve_recent_turns: 3,
            },
        };

        assert_eq!(
            registry.execute("/compact", &mut agent),
            tr(
                "Conversation too small to compact: current context is below the compaction threshold.",
            )
        );
    }
}
