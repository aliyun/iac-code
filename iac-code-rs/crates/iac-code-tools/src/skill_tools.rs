use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Arc;

use iac_code_protocol::message::AgentMessage;

use crate::agent_tool::SubAgentRunner;
use crate::{ToolContext, ToolRegistry};

mod auto_trigger;
mod bundled;
mod discovery;
mod frontmatter;
mod listing;
mod names;
mod prompt;
mod settings;
mod tool;

use discovery::discover_skills;
use listing::build_skill_listing;
use names::normalize_skill_name;
use prompt::render_skill_prompt;
use settings::enabled_only;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SkillSource {
    User,
    Project,
    Bundled,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SkillDefinition {
    pub name: String,
    pub description: String,
    pub allowed_tools: Vec<String>,
    pub when_to_use: String,
    pub arguments: Vec<String>,
    pub content: String,
    pub source: SkillSource,
    pub file_path: String,
    pub skill_root: String,
    pub user_invocable: bool,
    pub model_override: String,
    pub effort_override: String,
    pub context: String,
    pub agent: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SkillInvocation {
    pub skill_name: String,
    pub prompt: String,
    pub allowed_tools: Vec<String>,
    pub model_override: String,
    pub effort_override: String,
    pub is_fork: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SkillManager {
    skills: Vec<SkillDefinition>,
}

impl SkillManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn discover(
        user_skills_dir: impl AsRef<Path>,
        cwd: impl AsRef<Path>,
    ) -> Result<Self, String> {
        discover_skills(user_skills_dir.as_ref(), cwd.as_ref())
    }

    pub fn get(&self, name: &str) -> Option<&SkillDefinition> {
        let normalized = normalize_skill_name(name);
        self.skills
            .iter()
            .find(|skill| normalize_skill_name(&skill.name) == normalized)
    }

    pub fn skills(&self) -> &[SkillDefinition] {
        &self.skills
    }

    pub fn enabled_only(&self, disabled_skill_names: &BTreeSet<String>) -> Self {
        enabled_only(self, disabled_skill_names)
    }

    pub fn build_listing(&self) -> String {
        build_skill_listing(&self.skills)
    }

    pub fn render_user_invocable_skill(
        &self,
        name: &str,
        args: &str,
        context: &ToolContext,
    ) -> Result<Option<SkillInvocation>, String> {
        let Some(skill) = self.get(name) else {
            return Ok(None);
        };
        if !skill.user_invocable {
            return Err(format!("Skill '{}' is not user invocable.", skill.name));
        }
        let rendered_prompt = render_skill_prompt(skill, args, context);
        if skill.context == "fork" {
            return Ok(Some(SkillInvocation {
                skill_name: skill.name.clone(),
                prompt: rendered_prompt,
                allowed_tools: Vec::new(),
                model_override: skill.model_override.clone(),
                effort_override: skill.effort_override.clone(),
                is_fork: true,
            }));
        }

        Ok(Some(SkillInvocation {
            skill_name: skill.name.clone(),
            prompt: format!(
                "<skill-name>{}</skill-name>\n\n{}",
                skill.name, rendered_prompt
            ),
            allowed_tools: skill.allowed_tools.clone(),
            model_override: skill.model_override.clone(),
            effort_override: skill.effort_override.clone(),
            is_fork: false,
        }))
    }

    pub fn auto_triggered_messages(
        &self,
        prompt: &str,
        context: &ToolContext,
        existing_messages: &[AgentMessage],
    ) -> Vec<AgentMessage> {
        auto_trigger::auto_triggered_messages(self, prompt, context, existing_messages)
    }

    fn insert(&mut self, skill: SkillDefinition) {
        let normalized = normalize_skill_name(&skill.name);
        if let Some(existing) = self
            .skills
            .iter_mut()
            .find(|existing| normalize_skill_name(&existing.name) == normalized)
        {
            *existing = skill;
            return;
        }
        self.skills.push(skill);
    }
}

#[derive(Clone)]
pub struct SkillTool {
    manager: SkillManager,
    sub_agent_runner: Option<Arc<dyn SubAgentRunner>>,
}

impl SkillTool {
    pub fn new(manager: SkillManager) -> Self {
        Self {
            manager,
            sub_agent_runner: None,
        }
    }

    pub fn with_sub_agent_runner(mut self, runner: Arc<dyn SubAgentRunner>) -> Self {
        self.sub_agent_runner = Some(runner);
        self
    }
}

pub fn register_skill_tools(registry: &mut ToolRegistry, manager: SkillManager) {
    registry.register(Box::new(SkillTool::new(manager)));
}
