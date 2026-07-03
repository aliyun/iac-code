use std::collections::BTreeSet;

use iac_code_protocol::permission::{PermissionRuleValue, ToolPermissionContext};
use iac_code_protocol::PermissionRequestEvent;

use crate::permissions::{
    extract_permission_suggestions, PermissionOption, PermissionOutcome, PermissionToolCall,
    OPTION_ALLOW_ALWAYS, OPTION_ALLOW_ONCE, OPTION_REJECT_ALWAYS, OPTION_REJECT_ONCE,
    PREFIX_ALLOW_RULE, PREFIX_DENY_RULE,
};
use crate::session::{AcpClient, PermissionDecision};

pub(super) struct PermissionRequester<'a> {
    pub(super) session_id: &'a str,
    pub(super) client: &'a mut dyn AcpClient,
    pub(super) cache: &'a mut PermissionCache,
    pub(super) permission_context: &'a mut Option<ToolPermissionContext>,
    pub(super) blanket_allow_disabled_tools: &'a BTreeSet<String>,
}

impl PermissionRequester<'_> {
    pub(super) fn request(&mut self, event: PermissionRequestEvent) -> PermissionDecision {
        if let Some(decision) = self.cache.lookup(&event.tool_name) {
            return (decision == "always_allow").into();
        }

        let suggestions = extract_permission_suggestions(event.permission_result.as_ref());
        let options = self.build_options(&event.tool_name, &suggestions);
        let content_text = permission_content_text(&event, &suggestions);
        let response = self.client.request_permission(
            self.session_id,
            options,
            PermissionToolCall {
                tool_call_id: format!("permission/{}", event.tool_use_id),
                title: event.tool_name.clone(),
                content: vec![content_text],
            },
        );

        match &response.outcome {
            PermissionOutcome::Allowed { .. } => {
                if response.selected_option_id() == Some(OPTION_ALLOW_ALWAYS) {
                    self.cache
                        .record(event.tool_name.clone(), "always_allow".to_owned());
                } else if let Some(rule) = response
                    .selected_option_id()
                    .and_then(|option| option.strip_prefix(PREFIX_ALLOW_RULE))
                {
                    self.apply_rule(&event.tool_name, rule, RuleBehavior::Allow);
                }
                PermissionDecision::Allow
            }
            PermissionOutcome::Denied { .. } => {
                if response.selected_option_id() == Some(OPTION_REJECT_ALWAYS) {
                    self.cache
                        .record(event.tool_name.clone(), "always_deny".to_owned());
                } else if let Some(rule) = response
                    .selected_option_id()
                    .and_then(|option| option.strip_prefix(PREFIX_DENY_RULE))
                {
                    self.apply_rule(&event.tool_name, rule, RuleBehavior::Deny);
                }
                PermissionDecision::Deny
            }
            PermissionOutcome::Cancelled => PermissionDecision::Cancel,
        }
    }

    fn build_options(
        &self,
        tool_name: &str,
        suggestions: &[PermissionRuleValue],
    ) -> Vec<PermissionOption> {
        let mut options = vec![PermissionOption::new(
            OPTION_ALLOW_ONCE,
            "Allow once",
            "allow_once",
        )];

        if suggestions.is_empty() {
            if !self.blanket_allow_disabled_tools.contains(tool_name) {
                options.push(PermissionOption::new(
                    OPTION_ALLOW_ALWAYS,
                    "Always allow this tool",
                    "allow_always",
                ));
            }
        } else {
            let rules_display = rules_display(suggestions);
            options.push(PermissionOption::new(
                format!("{PREFIX_ALLOW_RULE}{rules_display}"),
                format!("Always allow \"{rules_display}\" (this session)"),
                "allow_always",
            ));
        }

        options.push(PermissionOption::new(
            OPTION_REJECT_ONCE,
            "Reject once",
            "reject_once",
        ));

        if !suggestions.is_empty() {
            let rules_display = rules_display(suggestions);
            options.push(PermissionOption::new(
                format!("{PREFIX_DENY_RULE}{rules_display}"),
                format!("Always deny \"{rules_display}\" (this session)"),
                "reject_always",
            ));
        }

        options.push(PermissionOption::new(
            OPTION_REJECT_ALWAYS,
            "Always reject this tool",
            "reject_always",
        ));
        options
    }

    fn apply_rule(&mut self, tool_name: &str, rules: &str, behavior: RuleBehavior) {
        let Some(context) = self.permission_context.as_mut() else {
            return;
        };
        let target = match behavior {
            RuleBehavior::Allow => &mut context.allow_rules,
            RuleBehavior::Deny => &mut context.deny_rules,
        };
        for rule in rules
            .split(',')
            .map(str::trim)
            .filter(|rule| !rule.is_empty())
        {
            target
                .entry("session".to_owned())
                .or_default()
                .push(format!("{tool_name}({rule})"));
        }
    }
}

enum RuleBehavior {
    Allow,
    Deny,
}

#[derive(Clone, Debug)]
pub(super) struct PermissionCache {
    entries: Vec<(String, String)>,
    max_size: usize,
}

impl PermissionCache {
    pub(super) fn new(max_size: usize) -> Self {
        Self {
            entries: Vec::new(),
            max_size,
        }
    }

    pub(super) fn lookup(&mut self, tool_name: &str) -> Option<String> {
        let index = self
            .entries
            .iter()
            .position(|(cached_tool, _)| cached_tool == tool_name)?;
        let entry = self.entries.remove(index);
        let decision = entry.1.clone();
        self.entries.push(entry);
        Some(decision)
    }

    pub(super) fn record(&mut self, tool_name: String, decision: String) {
        if let Some(index) = self
            .entries
            .iter()
            .position(|(cached_tool, _)| cached_tool == &tool_name)
        {
            self.entries.remove(index);
        }
        self.entries.push((tool_name, decision));
        self.evict_lru();
    }

    pub(super) fn clear(&mut self) {
        self.entries.clear();
    }

    pub(super) fn set_max_size(&mut self, max_size: usize) {
        self.max_size = max_size;
        self.evict_lru();
    }

    pub(super) fn snapshot(&self) -> Vec<(String, String)> {
        self.entries.clone()
    }

    fn evict_lru(&mut self) {
        while self.entries.len() > self.max_size {
            self.entries.remove(0);
        }
    }
}

fn permission_content_text(
    event: &PermissionRequestEvent,
    suggestions: &[PermissionRuleValue],
) -> String {
    let mut text = format!(
        "Approve tool call: {}\nInput: {}",
        event.tool_name,
        event.tool_input.to_compact_json()
    );
    if !suggestions.is_empty() {
        text.push_str(&format!("\nSuggested rule: {}", rules_display(suggestions)));
    }
    text
}

fn rules_display(suggestions: &[PermissionRuleValue]) -> String {
    suggestions
        .iter()
        .map(|suggestion| suggestion.rule_content.as_str())
        .collect::<Vec<_>>()
        .join(",")
}
