use crate::SuggestionItem;

use super::CommandSuggestionProvider;

impl CommandSuggestionProvider {
    pub(super) fn memory_argument_suggestions(&self, query: &str) -> Vec<SuggestionItem> {
        let arg_text = query["memory-folder".len()..].trim_start_matches(char::is_whitespace);
        let has_trailing_space =
            !arg_text.is_empty() && arg_text.chars().last().is_some_and(char::is_whitespace);
        let parts = arg_text.split_whitespace().collect::<Vec<_>>();

        if parts.is_empty() {
            return self.memory_first_argument_suggestions("");
        }

        let action = parts[0].to_lowercase();
        if action == "delete" && (has_trailing_space || parts.len() > 1) {
            let prefix = parts.get(1).copied().unwrap_or_default();
            return self.memory_name_suggestions(prefix, "/memory-folder delete ");
        }

        if action == "search" && has_trailing_space {
            return Vec::new();
        }

        if parts.len() == 1 && !has_trailing_space {
            return self.memory_first_argument_suggestions(parts[0]);
        }

        Vec::new()
    }

    fn memory_first_argument_suggestions(&self, prefix: &str) -> Vec<SuggestionItem> {
        let mut suggestions = Vec::new();
        for (name, description, completion) in [
            ("search", "Search saved memories", "/memory-folder search "),
            ("delete", "Delete a saved memory", "/memory-folder delete "),
            ("help", "Show memory command help", "/memory-folder help"),
        ] {
            if matches_prefix(name, prefix) {
                suggestions.push(memory_action_item(name, description, completion));
            }
        }
        suggestions.extend(self.memory_name_suggestions(prefix, "/memory-folder "));
        suggestions
    }

    fn memory_name_suggestions(&self, prefix: &str, command_prefix: &str) -> Vec<SuggestionItem> {
        let Some(source) = &self.memory_source else {
            return Vec::new();
        };
        let Ok(memories) = source.list_memories() else {
            return Vec::new();
        };

        let mut suggestions = memories
            .into_iter()
            .filter(|memory| !memory.name.is_empty() && matches_prefix(&memory.name, prefix))
            .map(|memory| SuggestionItem {
                id: format!("cmd:memory:{}", memory.name),
                display_text: memory.name.clone(),
                completion: format!("{command_prefix}{}", memory.name),
                description: Some(if memory.description.is_empty() {
                    "Saved memory".to_owned()
                } else {
                    memory.description
                }),
                icon: Some("/".to_owned()),
                source: "command".to_owned(),
                score: 500.0 - memory.name.chars().count() as f64,
                arg_hint: None,
            })
            .collect::<Vec<_>>();
        suggestions.sort_by(|left, right| left.display_text.cmp(&right.display_text));
        suggestions
    }
}

pub(super) fn is_memory_argument_query(query: &str) -> bool {
    query
        .strip_prefix("memory-folder")
        .and_then(|tail| tail.chars().next())
        .is_some_and(char::is_whitespace)
}

fn memory_action_item(name: &str, description: &str, completion: &str) -> SuggestionItem {
    SuggestionItem {
        id: format!("cmd:memory:{name}"),
        display_text: name.to_owned(),
        completion: completion.to_owned(),
        description: Some(description.to_owned()),
        icon: Some("/".to_owned()),
        source: "command".to_owned(),
        score: 1000.0 - name.chars().count() as f64,
        arg_hint: None,
    }
}

fn matches_prefix(value: &str, prefix: &str) -> bool {
    value.to_lowercase().starts_with(&prefix.to_lowercase())
}
