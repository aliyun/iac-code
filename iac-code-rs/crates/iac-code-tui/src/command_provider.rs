use crate::{CompletionToken, SuggestionItem, SuggestionProvider};

mod catalog;
mod fuzzy;
mod memory;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommandDefinition {
    pub name: String,
    pub description: String,
    pub aliases: Vec<String>,
    pub hidden: bool,
    pub arg_hint: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct FuzzyMatch {
    pub command: CommandDefinition,
    pub name: String,
    pub priority: u8,
    pub score: f64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CommandCatalog {
    commands: Vec<CommandDefinition>,
}

impl CommandCatalog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, command: CommandDefinition) {
        if let Some(existing) = self
            .commands
            .iter_mut()
            .find(|existing| existing.name == command.name)
        {
            *existing = command;
            return;
        }
        self.commands.push(command);
    }

    pub fn get_all(&self) -> Vec<CommandDefinition> {
        let mut commands = self
            .commands
            .iter()
            .filter(|command| !command.hidden)
            .cloned()
            .collect::<Vec<_>>();
        commands.sort_by(|left, right| left.name.cmp(&right.name));
        commands
    }

    pub fn find(&self, name_or_alias: &str) -> Option<CommandDefinition> {
        let normalized = name_or_alias.trim().to_ascii_lowercase();
        if normalized.is_empty() {
            return None;
        }
        self.commands
            .iter()
            .find(|command| {
                command.name.eq_ignore_ascii_case(&normalized)
                    || command
                        .aliases
                        .iter()
                        .any(|alias| alias.eq_ignore_ascii_case(&normalized))
            })
            .cloned()
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MemorySuggestionEntry {
    pub name: String,
    pub description: String,
}

pub trait MemorySuggestionSource {
    fn list_memories(&self) -> Result<Vec<MemorySuggestionEntry>, String>;
}

pub struct CommandSuggestionProvider {
    catalog: CommandCatalog,
    memory_source: Option<Box<dyn MemorySuggestionSource>>,
}

impl CommandSuggestionProvider {
    pub fn new(catalog: CommandCatalog) -> Self {
        Self {
            catalog,
            memory_source: None,
        }
    }

    pub fn default_commands() -> Self {
        Self::new(CommandCatalog::default_commands())
    }

    pub fn with_memory_source(mut self, source: Box<dyn MemorySuggestionSource>) -> Self {
        self.memory_source = Some(source);
        self
    }
}

impl SuggestionProvider for CommandSuggestionProvider {
    fn trigger(&self) -> &str {
        "/"
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        let query = token.text.strip_prefix('/').unwrap_or(&token.text);
        if memory::is_memory_argument_query(query) {
            return self.memory_argument_suggestions(query);
        }

        self.catalog
            .fuzzy_search(query)
            .into_iter()
            .map(|match_| {
                let completion = format!("/{} ", match_.name);
                SuggestionItem {
                    id: format!("cmd:{}", match_.command.name),
                    display_text: match_.name,
                    completion,
                    description: Some(match_.command.description),
                    icon: Some("/".to_owned()),
                    source: "command".to_owned(),
                    score: -(match_.priority as f64) * 1000.0 - match_.score,
                    arg_hint: match_.command.arg_hint,
                }
            })
            .collect()
    }
}
