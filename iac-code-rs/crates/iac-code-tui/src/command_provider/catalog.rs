use super::{CommandCatalog, CommandDefinition};

impl CommandCatalog {
    pub fn default_commands() -> Self {
        let mut catalog = Self::new();
        catalog.register(command("help", "Show available commands", &["?"], None));
        catalog.register(command("clear", "Clear conversation history", &[], None));
        catalog.register(command("model", "Show or switch model", &[], None));
        catalog.register(command(
            "effort",
            "Show or switch thinking effort",
            &[],
            None,
        ));
        catalog.register(command(
            "compact",
            "Compact conversation context",
            &[],
            None,
        ));
        catalog.register(command(
            "exit",
            "Exit the application",
            &["quit", "q"],
            None,
        ));
        catalog.register(command(
            "auth",
            "Authenticate with LLM provider",
            &["login"],
            None,
        ));
        catalog.register(command(
            "debug",
            "Toggle debug logging",
            &[],
            Some("[on|off]"),
        ));
        catalog.register(hidden_command(
            "memory-folder",
            "View and manage persistent memories",
            &[],
            Some("[<name>|search <query>|delete <name>|help]"),
        ));
        catalog.register(command("memory", "Edit memory files", &[], None));
        catalog.register(command(
            "resume",
            "Resume a previous session",
            &[],
            Some("[conversation id or search term]"),
        ));
        catalog.register(command(
            "rename",
            "Rename the current session",
            &[],
            Some("<name>"),
        ));
        catalog.register(command("skills", "Manage skills", &[], None));
        catalog.register(command("status", "Show current session status", &[], None));
        catalog
    }
}

fn command(
    name: &str,
    description: &str,
    aliases: &[&str],
    arg_hint: Option<&str>,
) -> CommandDefinition {
    CommandDefinition {
        name: name.to_owned(),
        description: description.to_owned(),
        aliases: aliases.iter().map(|alias| (*alias).to_owned()).collect(),
        hidden: false,
        arg_hint: arg_hint.map(str::to_owned),
    }
}

fn hidden_command(
    name: &str,
    description: &str,
    aliases: &[&str],
    arg_hint: Option<&str>,
) -> CommandDefinition {
    let mut command = command(name, description, aliases, arg_hint);
    command.hidden = true;
    command
}
