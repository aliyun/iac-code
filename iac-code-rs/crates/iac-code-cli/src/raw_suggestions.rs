use std::path::{Path, PathBuf};

use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::load_disabled_skills;
use iac_code_tools::{MemoryManager, SkillManager};
use iac_code_tui::{
    CommandCatalog, CommandSuggestionProvider, DirectorySuggestionProvider, FileSuggestionProvider,
    MemorySuggestionEntry, MemorySuggestionSource, ShellHistoryProvider, SkillCatalog,
    SkillDefinition as TuiSkillDefinition, SkillSuggestionProvider, SuggestionProvider,
};

use super::cli_i18n::tr_dynamic;

#[cfg(unix)]
pub(super) fn raw_interactive_suggestion_providers(
    root: &Path,
    skill_catalog: SkillCatalog,
) -> Vec<Box<dyn SuggestionProvider>> {
    vec![
        Box::new(raw_interactive_command_suggestion_provider()),
        Box::new(DirectorySuggestionProvider::new(root)),
        Box::new(FileSuggestionProvider::new(root)),
        Box::new(ShellHistoryProvider::new()),
        Box::new(SkillSuggestionProvider::new(skill_catalog)),
    ]
}

#[cfg(unix)]
pub(super) fn raw_interactive_command_suggestion_provider() -> CommandSuggestionProvider {
    let provider = CommandSuggestionProvider::new(localized_command_catalog());
    let Ok(paths) = ConfigPaths::from_env() else {
        return provider;
    };
    provider.with_memory_source(Box::new(ConfigMemorySuggestionSource {
        memory_dir: paths.subdirs().memory,
    }))
}

#[cfg(unix)]
pub(super) fn localized_command_catalog() -> CommandCatalog {
    let mut catalog = CommandCatalog::new();
    for mut command in CommandCatalog::default_commands().get_all() {
        command.description = tr_dynamic(&command.description);
        catalog.register(command);
    }
    catalog
}

#[cfg(unix)]
pub(super) struct ConfigMemorySuggestionSource {
    pub(super) memory_dir: PathBuf,
}

#[cfg(unix)]
impl MemorySuggestionSource for ConfigMemorySuggestionSource {
    fn list_memories(&self) -> Result<Vec<MemorySuggestionEntry>, String> {
        MemoryManager::new(self.memory_dir.clone())
            .map_err(|error| error.to_string())?
            .list_memories()
            .map(|memories| {
                memories
                    .into_iter()
                    .map(|memory| MemorySuggestionEntry {
                        name: memory.name,
                        description: memory.description,
                    })
                    .collect()
            })
    }
}

#[cfg(unix)]
pub(super) fn raw_interactive_skill_catalog(root: &Path) -> SkillCatalog {
    let Ok(paths) = ConfigPaths::from_env() else {
        return SkillCatalog::new();
    };
    let Ok(discovered) = SkillManager::discover(paths.subdirs().skills, root) else {
        return SkillCatalog::new();
    };
    let disabled = load_disabled_skills(&paths).unwrap_or_default();
    let enabled = discovered.enabled_only(&disabled);
    let mut catalog = SkillCatalog::new();
    for skill in enabled.skills().iter().filter(|skill| skill.user_invocable) {
        catalog.register(TuiSkillDefinition {
            name: skill.name.clone(),
            description: skill.description.clone(),
            aliases: Vec::new(),
            hidden: false,
        });
    }
    catalog
}
