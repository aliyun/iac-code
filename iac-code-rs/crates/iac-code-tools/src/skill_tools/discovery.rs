use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use super::bundled::bundled_skills;
use super::frontmatter::parse_frontmatter;
use super::{SkillDefinition, SkillManager, SkillSource};

pub(super) fn discover_skills(user_skills_dir: &Path, cwd: &Path) -> Result<SkillManager, String> {
    let mut manager = SkillManager::new();
    scan_skills_dir(&mut manager, user_skills_dir, SkillSource::User)?;

    for project_dir in find_project_skills_dirs(cwd) {
        scan_skills_dir(&mut manager, &project_dir, SkillSource::Project)?;
    }

    for skill in bundled_skills(user_skills_dir)? {
        manager.insert(skill);
    }
    Ok(manager)
}

fn scan_skills_dir(
    manager: &mut SkillManager,
    skills_dir: &Path,
    source: SkillSource,
) -> Result<(), String> {
    if !skills_dir.is_dir() {
        return Ok(());
    }

    let mut entries = fs::read_dir(skills_dir)
        .map_err(|error| error.to_string())?
        .filter_map(Result::ok)
        .collect::<Vec<_>>();
    entries.sort_by_key(|entry| entry.file_name());

    let mut seen_real_paths = BTreeSet::new();
    for entry in entries {
        let entry_path = entry.path();
        let real_path = entry_path
            .canonicalize()
            .unwrap_or_else(|_| entry_path.clone());
        if !seen_real_paths.insert(real_path.to_string_lossy().into_owned()) {
            continue;
        }
        if !entry_path.is_dir() {
            continue;
        }
        let skill_file = entry_path.join("SKILL.md");
        if !skill_file.is_file() {
            continue;
        }
        if let Some(skill) = load_skill_from_path(
            &skill_file,
            &entry.file_name().to_string_lossy(),
            source.clone(),
        ) {
            manager.insert(skill);
        }
    }
    Ok(())
}

fn find_project_skills_dirs(cwd: &Path) -> Vec<PathBuf> {
    let current = cwd.canonicalize().unwrap_or_else(|_| cwd.to_path_buf());
    let search_dirs = project_search_dirs(&current, find_git_root(&current));
    let mut result = Vec::new();
    for dir in search_dirs {
        let bare = dir.join("skills");
        if bare.is_dir() {
            result.push(bare);
        }
        let dotdir = dir.join(".iac-code").join("skills");
        if dotdir.is_dir() {
            result.push(dotdir);
        }
    }
    result
}

fn project_search_dirs(cwd: &Path, git_root: Option<PathBuf>) -> Vec<PathBuf> {
    let Some(git_root) = git_root else {
        return vec![cwd.to_path_buf()];
    };
    if !cwd.starts_with(&git_root) {
        return vec![cwd.to_path_buf()];
    }

    let mut dirs = Vec::new();
    let mut current = cwd.to_path_buf();
    loop {
        dirs.push(current.clone());
        if current == git_root {
            break;
        }
        if !current.pop() {
            break;
        }
    }
    dirs.reverse();
    dirs
}

fn find_git_root(cwd: &Path) -> Option<PathBuf> {
    cwd.ancestors()
        .find(|candidate| candidate.join(".git").exists())
        .map(Path::to_path_buf)
}

fn load_skill_from_path(
    file_path: &Path,
    skill_name: &str,
    source: SkillSource,
) -> Option<SkillDefinition> {
    let text = fs::read_to_string(file_path).ok()?;
    let (frontmatter, content) = parse_frontmatter(&text);
    let name = if frontmatter.name.is_empty() {
        skill_name.to_owned()
    } else {
        frontmatter.name.clone()
    };
    let skill_root = file_path
        .parent()
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_default();

    Some(SkillDefinition {
        name,
        description: frontmatter.description,
        allowed_tools: frontmatter.allowed_tools,
        when_to_use: frontmatter.when_to_use,
        arguments: frontmatter.arguments,
        content,
        source,
        file_path: file_path.to_string_lossy().into_owned(),
        skill_root,
        user_invocable: frontmatter.user_invocable,
        model_override: frontmatter.model,
        effort_override: frontmatter.effort,
        context: if frontmatter.context.is_empty() {
            "inline".into()
        } else {
            frontmatter.context
        },
        agent: if frontmatter.agent.is_empty() {
            "general-purpose".into()
        } else {
            frontmatter.agent
        },
    })
}
