use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};
use iac_code_protocol::StreamEvent;

use crate::agent_tool::SubAgentRequest;
use crate::{Tool, ToolContext, ToolContextModifier, ToolResult};

use super::names::normalize_skill_name;
use super::prompt::{contains_shell_commands, render_skill_prompt};
use super::{SkillDefinition, SkillSource, SkillTool};

impl Tool for SkillTool {
    fn name(&self) -> &str {
        "skill"
    }

    fn description(&self) -> &str {
        "Execute a skill within the current conversation."
    }

    fn input_schema(&self) -> JsonValue {
        json::object([
            ("type", json::string("object")),
            (
                "properties",
                json::object([
                    (
                        "skill",
                        json::object([
                            ("type", json::string("string")),
                            ("description", json::string("The skill name to execute.")),
                        ]),
                    ),
                    (
                        "args",
                        json::object([
                            ("type", json::string("string")),
                            (
                                "description",
                                json::string("Optional arguments for the skill."),
                            ),
                        ]),
                    ),
                ]),
            ),
            ("required", json::array([json::string("skill")])),
        ])
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        match string_field(input, "skill") {
            Some(_) => Ok(()),
            None => Err("missing required field 'skill'".into()),
        }
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let skill_name = normalize_skill_name(string_field(input, "skill").unwrap_or_default());
        let Some(skill) = self.manager.get(&skill_name) else {
            return ToolResult::error(format!("Skill not found: '{skill_name}'"));
        };
        let args = string_field(input, "args").unwrap_or_default();
        let prompt = render_skill_prompt(skill, args, context);
        if skill.context == "fork" {
            return self.execute_forked_skill(skill, skill_name, prompt, context);
        }
        execute_inline_skill(skill, prompt)
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Skill".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        _context: &ToolPermissionContext,
    ) -> PermissionResult {
        let skill_name = normalize_skill_name(string_field(input, "skill").unwrap_or_default());
        let Some(skill) = self.manager.get(&skill_name) else {
            return PermissionResult {
                behavior: "deny".into(),
                message: format!("Skill not found: {skill_name}"),
                reason: None,
                suggestions: None,
            };
        };

        if skill.source == SkillSource::Bundled
            || (skill.allowed_tools.is_empty() && !contains_shell_commands(&skill.content))
        {
            return PermissionResult::allow();
        }

        PermissionResult::ask(format!(
            "Allow skill '{}' (source: {:?})?",
            skill.name, skill.source
        ))
    }
}

impl SkillTool {
    fn execute_forked_skill(
        &self,
        skill: &SkillDefinition,
        skill_name: String,
        prompt: String,
        context: &ToolContext,
    ) -> ToolResult {
        let Some(runner) = &self.sub_agent_runner else {
            return ToolResult::error(format!(
                "Skill forked execution is not configured: '{skill_name}'"
            ));
        };
        match runner.run(SubAgentRequest {
            prompt,
            agent_type: skill.agent.clone(),
            cwd: context.cwd.clone(),
        }) {
            Ok(result) => ToolResult::success(format!(
                "{}\n\n[Skill '{}' completed: {} tool calls, {} tokens]",
                result.output,
                skill.name,
                result.progress.tool_use_count,
                result.progress.token_count
            ))
            .with_stream_events(
                result
                    .stream_events
                    .into_iter()
                    .map(StreamEvent::SubAgentTool)
                    .collect(),
            ),
            Err(error) => ToolResult::error(format!("Skill forked execution failed: {error}")),
        }
    }
}

fn execute_inline_skill(skill: &SkillDefinition, prompt: String) -> ToolResult {
    let tagged_content = format!("<skill-name>{}</skill-name>\n\n{}", skill.name, prompt);
    let result = ToolResult::success(format!("Skill '{}' loaded (inline).", skill.name))
        .with_new_messages(vec![json::object([
            ("role", json::string("user")),
            ("content", json::string(tagged_content)),
        ])]);
    let context_modifier = skill_context_modifier(skill);
    if context_modifier == ToolContextModifier::default() {
        result
    } else {
        result.with_context_modifier(context_modifier)
    }
}

fn skill_context_modifier(skill: &SkillDefinition) -> ToolContextModifier {
    ToolContextModifier {
        allowed_tool_rules: skill.allowed_tools.clone(),
        model_override: (!skill.model_override.is_empty() && skill.model_override != "inherit")
            .then(|| skill.model_override.clone()),
        effort_override: (!skill.effort_override.is_empty()).then(|| skill.effort_override.clone()),
    }
}

fn string_field<'a>(input: &'a JsonValue, key: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}
