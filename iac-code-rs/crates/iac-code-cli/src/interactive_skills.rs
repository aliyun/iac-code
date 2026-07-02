use std::collections::BTreeSet;

use iac_code_config::i18n::detect_language;
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{normalize_skill_name, save_disabled_skills};
use iac_code_tools::{SkillDefinition, SkillManager};

use crate::cli_i18n::{tr, tr_name};
use crate::session_utils::current_working_directory;
use crate::skills_management::{
    is_skill_enabled, is_skill_locked, load_skill_management_state, skill_source_label,
};

pub(super) fn interactive_skills_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let state = load_skill_management_state(&paths, &cwd)?;
    let manager = state.manager;
    let mut disabled = state.disabled;
    let locked = state.locked;
    let parts = args.split_whitespace().collect::<Vec<_>>();
    if parts.is_empty() || parts[0].eq_ignore_ascii_case("list") {
        return Ok(format_skill_management_list(&manager, &disabled));
    }

    match parts[0].to_ascii_lowercase().as_str() {
        "help" => Ok("Usage: /skills [list|enable <name>|disable <name>|help]".to_owned()),
        "enable" => {
            if parts.len() != 2 {
                return Ok("Usage: /skills [list|enable <name>|disable <name>|help]".to_owned());
            }
            let Some(skill) = manager.get(parts[1]) else {
                return Ok(format!("Skill '{}' not found.", parts[1]));
            };
            let name = skill.name.clone();
            disabled.remove(&normalize_skill_name(&name));
            save_disabled_skills(
                &paths,
                disabled.iter().map(String::as_str),
                locked.iter().map(String::as_str),
            )
            .map_err(|error| error.to_string())?;
            Ok(format_skill_enabled_message(&name))
        }
        "disable" => {
            if parts.len() != 2 {
                return Ok("Usage: /skills [list|enable <name>|disable <name>|help]".to_owned());
            }
            let Some(skill) = manager.get(parts[1]) else {
                return Ok(format!("Skill '{}' not found.", parts[1]));
            };
            let name = skill.name.clone();
            if is_skill_locked(skill) {
                return Ok(format_bundled_skill_cannot_be_disabled_message(&name));
            }
            disabled.insert(normalize_skill_name(&name));
            save_disabled_skills(
                &paths,
                disabled.iter().map(String::as_str),
                locked.iter().map(String::as_str),
            )
            .map_err(|error| error.to_string())?;
            Ok(format_skill_disabled_message(&name))
        }
        _ => Ok("Usage: /skills [list|enable <name>|disable <name>|help]".to_owned()),
    }
}

fn format_skill_management_list(manager: &SkillManager, disabled: &BTreeSet<String>) -> String {
    let mut skills = manager.skills().to_vec();
    skills.sort_by(|left, right| left.name.cmp(&right.name));
    let mut output = format!("{}:", tr("Skills"));
    for skill in skills {
        output.push_str("\n  - ");
        output.push_str(&format_skill_management_item(&skill, disabled));
    }
    output
}

fn format_skill_management_item(skill: &SkillDefinition, disabled: &BTreeSet<String>) -> String {
    let locked = is_skill_locked(skill);
    let enabled = is_skill_enabled(skill, disabled);
    let mut labels = vec![skill_enabled_label(enabled)];
    if locked {
        labels.push(tr("locked"));
    }
    labels.push(tr(skill_source_label(&skill.source)));
    format!(
        "{} [{}] - {}",
        skill.name,
        labels.join(", "),
        skill.description
    )
}

fn skill_enabled_label(enabled: bool) -> String {
    if detect_language() == "en" {
        return if enabled { "enabled" } else { "disabled" }.to_owned();
    }
    if enabled {
        tr("on")
    } else {
        tr("off")
    }
}

fn format_skill_enabled_message(name: &str) -> String {
    if detect_language() == "en" {
        return format!("Skill '{name}' enabled.");
    }
    format!("{name}: {}", tr("on"))
}

fn format_skill_disabled_message(name: &str) -> String {
    if detect_language() == "en" {
        return format!("Skill '{name}' disabled.");
    }
    tr_name("Skill disabled: {name}", name)
}

fn format_bundled_skill_cannot_be_disabled_message(name: &str) -> String {
    if detect_language() == "en" {
        return format!("Skill '{name}' is bundled and cannot be disabled.");
    }
    format!("{name}: {}", tr("Bundled skills cannot be disabled."))
}
