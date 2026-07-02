use iac_code_tui::{
    CommandCatalog, CommandSuggestionProvider, CompletionToken, MemorySuggestionEntry,
    MemorySuggestionSource, SuggestionProvider,
};

#[test]
fn default_command_provider_matches_python_command_suggestions() {
    let provider = CommandSuggestionProvider::default_commands();

    assert_eq!(provider.trigger(), "/");

    let empty_items = provider.provide(&token("/"));
    let empty_names = names(&empty_items);
    assert!(empty_names.contains(&"help"));
    assert!(empty_names.contains(&"model"));
    assert!(empty_names.contains(&"clear"));
    assert!(!empty_names.contains(&"tasks"));
    assert!(!empty_names.contains(&"memory-folder"));
    assert!(empty_items
        .iter()
        .all(|item| item.source == "command" && item.icon.as_deref() == Some("/")));
    assert!(empty_items.iter().all(|item| item.id.starts_with("cmd:")));

    let model_items = provider.provide(&token("/mod"));
    assert!(names(&model_items).contains(&"model"));

    assert!(provider
        .provide(&token("/tas"))
        .iter()
        .all(|item| item.display_text != "tasks"));

    let help_items = provider.provide(&token("/help"));
    let help = help_items
        .iter()
        .find(|item| item.display_text == "help")
        .expect("help suggestion should exist");
    assert_eq!(help.completion, "/help ");

    assert!(provider.provide(&token("/xyzabc")).is_empty());
}

#[test]
fn command_provider_preserves_arg_hints_and_alias_matches() {
    let provider = CommandSuggestionProvider::default_commands();

    let debug_items = provider.provide(&token("/debug"));
    let debug = debug_items
        .iter()
        .find(|item| item.display_text == "debug")
        .expect("debug suggestion should exist");
    assert_eq!(debug.arg_hint.as_deref(), Some("[on|off]"));

    let clear_items = provider.provide(&token("/clear"));
    let clear = clear_items
        .iter()
        .find(|item| item.display_text == "clear")
        .expect("clear suggestion should exist");
    assert_eq!(clear.arg_hint, None);

    let alias_items = provider.provide(&token("/login"));
    let auth = alias_items
        .iter()
        .find(|item| item.display_text == "login")
        .expect("auth alias suggestion should exist");
    assert_eq!(auth.id, "cmd:auth");
    assert_eq!(auth.completion, "/login ");
}

#[test]
fn command_catalog_resolves_visible_hidden_and_alias_commands() {
    let catalog = CommandCatalog::default_commands();

    let auth = catalog.find("login").expect("login alias should resolve");
    assert_eq!(auth.name, "auth");

    let memory_folder = catalog
        .find("memory-folder")
        .expect("hidden command should still resolve");
    assert!(memory_folder.hidden);

    assert!(catalog.find("tasks").is_none());
}

#[test]
fn command_catalog_fuzzy_search_matches_python_priorities() {
    let mut catalog = CommandCatalog::new();
    catalog.register(command("model", "Switch the AI model", &["md"], None));
    catalog.register(command("help", "Show help information", &["?", "h"], None));
    catalog.register(command("clear", "Clear the screen", &[], None));
    catalog.register(command("exit", "Exit", &["quit"], None));

    let exact = catalog.fuzzy_search("model");
    assert_eq!(exact[0].priority, 0);
    assert_eq!(exact[0].name, "model");

    let prefix = catalog.fuzzy_search("mo");
    assert_eq!(prefix[0].priority, 1);
    assert_eq!(prefix[0].name, "model");

    let exact_alias = catalog.fuzzy_search("md");
    assert_eq!(exact_alias[0].priority, 2);
    assert_eq!(exact_alias[0].name, "md");
    assert_eq!(exact_alias[0].command.name, "model");

    let alias_prefix = catalog.fuzzy_search("qu");
    assert_eq!(alias_prefix[0].priority, 3);
    assert_eq!(alias_prefix[0].name, "quit");
    assert_eq!(alias_prefix[0].command.name, "exit");

    let subsequence = catalog.fuzzy_search("mdl");
    assert_eq!(subsequence[0].priority, 4);
    assert_eq!(subsequence[0].command.name, "model");

    let description = catalog.fuzzy_search("screen");
    assert_eq!(description[0].priority, 5);
    assert_eq!(description[0].command.name, "clear");

    assert!(catalog.fuzzy_search("zzzzz").is_empty());
}

#[test]
fn memory_argument_suggestions_match_python_provider() {
    let provider = CommandSuggestionProvider::default_commands()
        .with_memory_source(Box::new(FixedMemorySource));

    let second_arg = provider.provide(&token("/memory-folder "));
    let second_arg_names = names(&second_arg);
    assert!(
        ["search", "delete", "help", "user-role", "feedback-testing"]
            .iter()
            .all(|name| second_arg_names.contains(name))
    );
    assert_eq!(
        completions_for(&second_arg, "search"),
        vec!["/memory-folder search "]
    );
    assert_eq!(
        completions_for(&second_arg, "user-role"),
        vec!["/memory-folder user-role"]
    );

    let delete_action = provider.provide(&token("/memory-folder d"));
    assert_eq!(names(&delete_action), vec!["delete"]);
    assert_eq!(delete_action[0].completion, "/memory-folder delete ");

    let delete_names = provider.provide(&token("/memory-folder delete "));
    assert_eq!(names(&delete_names), vec!["feedback-testing", "user-role"]);
    assert_eq!(
        delete_names[0].completion,
        "/memory-folder delete feedback-testing"
    );
    assert!(delete_names
        .iter()
        .all(|item| item.id.starts_with("cmd:memory:")));

    assert!(provider
        .provide(&token("/memory-folder search "))
        .is_empty());
}

fn token(text: &str) -> CompletionToken {
    CompletionToken {
        text: text.to_owned(),
        start: 0,
        end: text.len(),
        trigger: "/".to_owned(),
    }
}

fn command(
    name: &str,
    description: &str,
    aliases: &[&str],
    arg_hint: Option<&str>,
) -> iac_code_tui::CommandDefinition {
    iac_code_tui::CommandDefinition {
        name: name.to_owned(),
        description: description.to_owned(),
        aliases: aliases.iter().map(|alias| (*alias).to_owned()).collect(),
        hidden: false,
        arg_hint: arg_hint.map(str::to_owned),
    }
}

fn names(items: &[iac_code_tui::SuggestionItem]) -> Vec<&str> {
    items
        .iter()
        .map(|item| item.display_text.as_str())
        .collect()
}

fn completions_for<'a>(items: &'a [iac_code_tui::SuggestionItem], name: &str) -> Vec<&'a str> {
    items
        .iter()
        .filter(|item| item.display_text == name)
        .map(|item| item.completion.as_str())
        .collect()
}

struct FixedMemorySource;

impl MemorySuggestionSource for FixedMemorySource {
    fn list_memories(&self) -> Result<Vec<MemorySuggestionEntry>, String> {
        Ok(vec![
            MemorySuggestionEntry {
                name: "user-role".to_owned(),
                description: "Role".to_owned(),
            },
            MemorySuggestionEntry {
                name: "feedback-testing".to_owned(),
                description: "Testing".to_owned(),
            },
        ])
    }
}
