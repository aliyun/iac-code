use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{load_disabled_skills, normalize_skill_name};
use iac_code_exec::{HeadlessRunResult, OutputFormat};
use iac_code_tools::{SkillInvocation, SkillManager, SkillSource, TaskManager, ToolContext};

use crate::cli_args::{split_tool_rules, Cli};
use crate::cli_i18n::tr_name;
use crate::headless_runner::run_prompt_from_cli;
use crate::session_utils::current_working_directory;

pub(super) struct ParsedInteractiveSkillInvocation {
    pub(super) name: String,
    args: String,
}

pub(super) fn parse_interactive_skill_invocation(
    prompt: &str,
) -> Option<ParsedInteractiveSkillInvocation> {
    let prompt = prompt.trim();
    if prompt.len() <= 1 || !(prompt.starts_with('$') || prompt.starts_with('/')) {
        return None;
    }
    let mut parts = prompt[1..].splitn(2, char::is_whitespace);
    let name = parts.next().unwrap_or_default().trim();
    if name.is_empty() {
        return None;
    }
    Some(ParsedInteractiveSkillInvocation {
        name: name.to_owned(),
        args: parts.next().unwrap_or_default().trim().to_owned(),
    })
}

pub(super) fn run_interactive_skill_from_cli(
    cli: &Cli,
    prompt: &str,
    output_format: OutputFormat,
    resume: &str,
    continue_session: bool,
    task_manager: TaskManager,
) -> Result<Option<HeadlessRunResult>, String> {
    let Some(invocation) = parse_interactive_skill_invocation(prompt) else {
        return Ok(None);
    };
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let discovered = SkillManager::discover(paths.subdirs().skills, &cwd)?;
    let disabled = load_disabled_skills(&paths).map_err(|error| error.to_string())?;
    if let Some(skill) = discovered.get(&invocation.name) {
        if skill.source != SkillSource::Bundled
            && disabled.contains(&normalize_skill_name(&skill.name))
        {
            return Err(tr_name(
                "Skill '{name}' is disabled. Run /skills to enable it.",
                &skill.name,
            ));
        }
    }
    let skill_manager = discovered.enabled_only(&disabled);
    let Some(skill_invocation) = skill_manager.render_user_invocable_skill(
        &invocation.name,
        &invocation.args,
        &ToolContext { cwd },
    )?
    else {
        return Ok(None);
    };
    let skill_cli = if skill_invocation.is_fork {
        cli.clone()
    } else {
        cli.with_allowed_tools(merge_allowed_tools(&cli.allowed_tools, &skill_invocation))
    };
    run_prompt_from_cli(
        &skill_cli,
        &skill_invocation.prompt,
        output_format,
        resume,
        continue_session,
        Some(task_manager),
    )
    .map(Some)
}

fn merge_allowed_tools(existing: &str, invocation: &SkillInvocation) -> String {
    let mut rules = split_tool_rules(existing);
    for rule in &invocation.allowed_tools {
        let rule = rule.trim();
        if !rule.is_empty() && !rules.iter().any(|existing| existing == rule) {
            rules.push(rule.to_owned());
        }
    }
    rules.join(",")
}
