use iac_code_config::paths::ConfigPaths;
use iac_code_tools::{Memory, MemoryManager};

use crate::cli_i18n::{tr, tr_name};
use crate::interactive_commands::print_interactive_command_result;
use crate::raw_memory::{
    format_memory_dialog_summary, is_auto_memory_enabled, memory_runtime_paths,
};
use crate::session_utils::current_working_directory;

pub(super) fn print_interactive_memory(args: &str) {
    let message = match interactive_memory_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn interactive_memory_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    if args.trim().is_empty() {
        let runtime = memory_runtime_paths(&paths, &cwd);
        return Ok(format_memory_dialog_summary(
            &runtime,
            is_auto_memory_enabled(&paths),
            0,
        ));
    }
    interactive_memory_folder_message(args)
}

pub(super) fn print_interactive_memory_folder(args: &str) {
    let message = match interactive_memory_folder_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn interactive_memory_folder_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let manager = MemoryManager::new(paths.subdirs().memory).map_err(|error| error.to_string())?;
    let parts = args.split_whitespace().collect::<Vec<_>>();
    if parts.is_empty() {
        let memories = manager.list_memories()?;
        let title = tr("Saved memories:");
        return Ok(format_memory_summary(&title, &memories)
            .unwrap_or_else(|| tr("No memories saved yet.")));
    }

    match parts[0].to_ascii_lowercase().as_str() {
        "help" => Ok(interactive_memory_usage()),
        "search" => {
            let query = parts[1..].join(" ");
            if query.trim().is_empty() {
                return Ok(interactive_memory_usage());
            }
            let memories = manager.search(&query)?;
            let title = tr("Matching memories:");
            Ok(format_memory_summary(&title, &memories)
                .unwrap_or_else(|| tr("No matching memories.")))
        }
        "delete" => {
            if parts.len() != 2 {
                return Ok(interactive_memory_usage());
            }
            let name = parts[1];
            if manager.load(name)?.is_none() {
                return Ok(tr_name("Memory '{name}' not found.", name));
            }
            manager.delete(name)?;
            Ok(tr_name("Memory '{name}' deleted.", name))
        }
        _ => {
            if parts.len() != 1 {
                return Ok(interactive_memory_usage());
            }
            let name = parts[0];
            match manager.load(name)? {
                Some(memory) => Ok(format_memory_detail(&memory)),
                None => Ok(tr_name("Memory '{name}' not found.", name)),
            }
        }
    }
}

fn interactive_memory_usage() -> String {
    tr("Usage: /memory-folder [<name>|search <query>|delete <name>|help]")
}

fn format_memory_summary(title: &str, memories: &[Memory]) -> Option<String> {
    if memories.is_empty() {
        return None;
    }
    let mut memories = memories.to_vec();
    memories.sort_by(|left, right| left.name.cmp(&right.name));
    let mut output = title.to_owned();
    for memory in memories {
        output.push_str(&format!("\n  - {} - {}", memory.name, memory.description));
    }
    Some(output)
}

fn format_memory_detail(memory: &Memory) -> String {
    format!(
        "[{}] {}\n\n{}",
        memory.memory_type, memory.description, memory.content
    )
}
