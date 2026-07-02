use std::collections::BTreeSet;

use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{load_disabled_skills, normalize_skill_name};
use iac_code_tools::{SkillDefinition, SkillManager, SkillSource};
use iac_code_tui::{SkillManagementItem, SkillManagementSource};

pub(super) struct SkillManagementState {
    pub(super) manager: SkillManager,
    pub(super) disabled: BTreeSet<String>,
    pub(super) locked: BTreeSet<String>,
    pub(super) items: Vec<SkillManagementItem>,
}

pub(super) fn load_skill_management_state(
    paths: &ConfigPaths,
    cwd: &str,
) -> Result<SkillManagementState, String> {
    let manager = SkillManager::discover(paths.subdirs().skills, cwd)?;
    let disabled = load_disabled_skills(paths).map_err(|error| error.to_string())?;
    Ok(skill_management_state(manager, disabled))
}

pub(super) fn skill_management_state(
    manager: SkillManager,
    disabled: BTreeSet<String>,
) -> SkillManagementState {
    let locked = locked_skill_names(&manager);
    let items = manager
        .skills()
        .iter()
        .map(|skill| skill_management_item(skill, &disabled))
        .collect();
    SkillManagementState {
        manager,
        disabled,
        locked,
        items,
    }
}

pub(super) fn locked_skill_names(manager: &SkillManager) -> BTreeSet<String> {
    manager
        .skills()
        .iter()
        .filter(|skill| is_skill_locked(skill))
        .map(|skill| normalize_skill_name(&skill.name))
        .collect()
}

pub(super) fn is_skill_locked(skill: &SkillDefinition) -> bool {
    skill.source == SkillSource::Bundled
}

pub(super) fn is_skill_enabled(skill: &SkillDefinition, disabled: &BTreeSet<String>) -> bool {
    is_skill_locked(skill) || !disabled.contains(&normalize_skill_name(&skill.name))
}

pub(super) fn skill_management_item(
    skill: &SkillDefinition,
    disabled: &BTreeSet<String>,
) -> SkillManagementItem {
    SkillManagementItem::new(
        skill.name.clone(),
        skill.description.clone(),
        skill_management_source(&skill.source),
        skill.content.chars().count(),
        skill.file_path.clone(),
        is_skill_enabled(skill, disabled),
        is_skill_locked(skill),
    )
}

pub(super) fn format_skill_token_estimate(content_length: usize) -> String {
    let tokens = content_length.div_ceil(4).max(1);
    if tokens >= 1000 {
        return crate::cli_i18n::tr("~{count}k tokens")
            .replace("{count}", &format!("{:.1}", tokens as f64 / 1000.0));
    }
    crate::cli_i18n::tr("~{count} tokens").replace("{count}", &tokens.to_string())
}

pub(super) fn skill_management_source(source: &SkillSource) -> SkillManagementSource {
    match source {
        SkillSource::Bundled => SkillManagementSource::Bundled,
        SkillSource::Project => SkillManagementSource::Project,
        SkillSource::User => SkillManagementSource::User,
    }
}

pub(super) fn skill_source_label(source: &SkillSource) -> &'static str {
    match source {
        SkillSource::User => "user",
        SkillSource::Project => "project",
        SkillSource::Bundled => "bundled",
    }
}
