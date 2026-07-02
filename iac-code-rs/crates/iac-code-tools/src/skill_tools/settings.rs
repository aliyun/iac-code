use std::collections::BTreeSet;

use super::names::normalize_skill_name;
use super::{SkillManager, SkillSource};

pub(super) fn enabled_only(
    manager: &SkillManager,
    disabled_skill_names: &BTreeSet<String>,
) -> SkillManager {
    let mut enabled = SkillManager::new();
    for skill in &manager.skills {
        let locked = skill.source == SkillSource::Bundled;
        if locked || !disabled_skill_names.contains(&normalize_skill_name(&skill.name)) {
            enabled.insert(skill.clone());
        }
    }
    enabled
}
