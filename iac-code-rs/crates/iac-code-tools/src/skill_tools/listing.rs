use super::{SkillDefinition, SkillSource};

const MAX_LISTING_DESC_CHARS: usize = 250;

pub(super) fn build_skill_listing(skills: &[SkillDefinition]) -> String {
    if skills.is_empty() {
        return String::new();
    }

    let mut lines = Vec::new();
    for skill in skills
        .iter()
        .filter(|skill| skill.source == SkillSource::Bundled)
        .chain(
            skills
                .iter()
                .filter(|skill| skill.source != SkillSource::Bundled),
        )
    {
        lines.push(format_skill_listing_line(skill));
    }

    format!(
        "The following skills are available for use with the Skill tool:\n{}",
        lines.join("\n")
    )
}

fn format_skill_listing_line(skill: &SkillDefinition) -> String {
    let mut description = skill.description.clone();
    if !skill.when_to_use.is_empty() {
        description.push('\n');
        description.push_str(&skill.when_to_use);
    }
    if description.len() > MAX_LISTING_DESC_CHARS {
        description.truncate(MAX_LISTING_DESC_CHARS - 3);
        description.push_str("...");
    }
    format!("- {}: {}", skill.name, description)
}
