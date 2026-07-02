use iac_code_config::paths::ConfigPaths;
use iac_code_protocol::json;
use iac_code_tools::{
    check_tool_permission, register_file_tools, RegistryToolExecutor, ToolCallRequest, ToolContext,
    ToolRegistry,
};

use crate::cli_args::Cli;
use crate::cli_i18n::tr;
use crate::permission_settings::load_tool_permission_context;
use crate::session_utils::current_working_directory;

pub(super) fn handle_interactive_shell_escape(cli: &Cli, prompt: &str) -> Result<(), String> {
    let command = prompt.strip_prefix('!').unwrap_or(prompt).trim();
    if command.is_empty() {
        println!("{}", tr("Usage: !<shell command>"));
        return Ok(());
    }

    let cwd = current_working_directory()?;
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);
    let tool_input = json::object([("command", json::string(command))]);
    let permission_context = load_tool_permission_context(
        &paths,
        &cli.allowed_tools,
        &cli.disallowed_tools,
        &cli.permission_mode,
        &cwd,
    )?;

    let Some(tool) = registry.get("bash") else {
        println!("{}", tr("Shell command support is unavailable."));
        return Ok(());
    };
    let permission = check_tool_permission(tool, &tool_input, &permission_context);
    match permission.behavior.as_str() {
        "allow" => {}
        "deny" => {
            let message = if permission.message.is_empty() {
                tr("Permission denied.")
            } else {
                permission.message
            };
            println!("{message}");
            return Ok(());
        }
        _ => {
            println!("{}", tr("Permission denied."));
            return Ok(());
        }
    }

    let executor = RegistryToolExecutor::new(registry)
        .with_context(ToolContext { cwd })
        .with_permission_context(permission_context);
    let request = ToolCallRequest {
        tool_use_id: "shell-escape".into(),
        tool_name: "bash".into(),
        input: tool_input,
    };
    let result = executor
        .execute_batch(&[request])
        .into_iter()
        .next()
        .unwrap_or_else(|| {
            iac_code_tools::ToolResult::error("Shell command did not return a result.")
        });

    println!("$ {command}");
    let output = result.content.trim_end();
    if !output.is_empty() {
        println!("{output}");
    }
    Ok(())
}
