use std::cell::Cell;
use std::collections::BTreeMap;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionMode, PermissionResult, PermissionRuleValue,
    ToolPermissionContext,
};
use iac_code_tools::{
    check_tool_permission, RegistryToolExecutor, Tool, ToolCallRequest, ToolContext, ToolExecutor,
    ToolRegistry, ToolResult,
};

#[test]
fn permission_pipeline_denies_bare_tool_rule_before_tool_checks() {
    let tool = PermissionTool::new("write_file", PermissionResult::allow());
    let context = context_with_rules(
        PermissionMode::BypassPermissions,
        &[("cli_arg", "write_file")],
        &[],
        &[],
    );

    let result = check_tool_permission(&tool, &empty_object(), &context);

    assert_eq!(result, PermissionResult::deny());
    assert_eq!(tool.checks.get(), 0);
}

#[test]
fn permission_pipeline_promotes_allowed_tool_when_ask_rule_matches() {
    let tool = PermissionTool::new("write_file", PermissionResult::allow());
    let context = context_with_rules(
        PermissionMode::Default,
        &[],
        &[("session", "write_file")],
        &[],
    );

    let result = check_tool_permission(&tool, &empty_object(), &context);

    assert_eq!(
        result,
        PermissionResult {
            behavior: "ask".into(),
            message: "matched ask rule(s): write_file".into(),
            reason: Some(PermissionDecisionReason {
                type_name: "rule".into(),
                detail: "matched ask rule(s): write_file".into(),
            }),
            suggestions: None,
        }
    );
}

#[test]
fn permission_pipeline_bypass_allows_normal_ask_but_not_safety_check() {
    let normal_ask = PermissionTool::new("bash", PermissionResult::ask("Allow Bash?"));
    let safety_ask = PermissionTool::new(
        "bash",
        PermissionResult {
            behavior: "ask".into(),
            message: "dangerous command".into(),
            reason: Some(PermissionDecisionReason {
                type_name: "safety_check".into(),
                detail: "dangerous command".into(),
            }),
            suggestions: None,
        },
    );
    let context = context_with_rules(PermissionMode::BypassPermissions, &[], &[], &[]);

    assert_eq!(
        check_tool_permission(&normal_ask, &empty_object(), &context),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(&safety_ask, &empty_object(), &context),
        PermissionResult {
            behavior: "ask".into(),
            message: "dangerous command".into(),
            reason: Some(PermissionDecisionReason {
                type_name: "safety_check".into(),
                detail: "dangerous command".into(),
            }),
            suggestions: None,
        }
    );
}

#[test]
fn permission_pipeline_allow_rule_respects_blanket_allow_support() {
    let supported = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"));
    let unsupported = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"))
        .without_blanket_allow();
    let context = context_with_rules(
        PermissionMode::Default,
        &[],
        &[],
        &[("session", "write_file")],
    );

    assert_eq!(
        check_tool_permission(&supported, &empty_object(), &context),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(&unsupported, &empty_object(), &context),
        PermissionResult::ask("Allow Write?")
    );
}

#[test]
fn permission_pipeline_passthrough_becomes_prompt_or_deny_in_dont_ask_mode() {
    let suggestion = PermissionRuleValue {
        tool_name: "write_file".into(),
        rule_content: "write_file".into(),
    };
    let passthrough = PermissionTool::new(
        "write_file",
        PermissionResult {
            behavior: "passthrough".into(),
            message: String::new(),
            reason: None,
            suggestions: Some(vec![suggestion.clone()]),
        },
    )
    .with_user_facing_name("Write");
    let default_context = context_with_rules(PermissionMode::Default, &[], &[], &[]);
    let dont_ask_context = context_with_rules(PermissionMode::DontAsk, &[], &[], &[]);

    assert_eq!(
        check_tool_permission(&passthrough, &empty_object(), &default_context),
        PermissionResult {
            behavior: "ask".into(),
            message: "Allow Write?".into(),
            reason: None,
            suggestions: Some(vec![suggestion]),
        }
    );
    assert_eq!(
        check_tool_permission(&passthrough, &empty_object(), &dont_ask_context),
        PermissionResult::deny()
    );
}

#[test]
fn registry_executor_denies_without_validating_or_executing_when_permission_denies() {
    let tool = PermissionTool::new("write_file", PermissionResult::allow());
    let counters = tool.counters.clone();
    let mut registry = ToolRegistry::new();
    registry.register(Box::new(tool));
    let permission_context = context_with_rules(
        PermissionMode::BypassPermissions,
        &[("cli_arg", "write_file")],
        &[],
        &[],
    );
    let executor = RegistryToolExecutor::new(registry).with_permission_context(permission_context);

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_1".into(),
        tool_name: "write_file".into(),
        input: json::object([("invalid", json::bool_value(true))]),
    });

    assert_eq!(result, ToolResult::error("Permission denied."));
    assert_eq!(counters.checks.get(), 0);
    assert_eq!(counters.validations.get(), 0);
    assert_eq!(counters.executions.get(), 0);
}

#[test]
fn registry_executor_applies_permission_modes_before_execution() {
    let ask_tool = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"));
    let denied_counters = ask_tool.counters.clone();
    let mut denied_registry = ToolRegistry::new();
    denied_registry.register(Box::new(ask_tool));
    let denied_executor = RegistryToolExecutor::new(denied_registry)
        .with_permission_context(context_with_rules(PermissionMode::DontAsk, &[], &[], &[]));

    let denied = denied_executor.execute(ToolCallRequest {
        tool_use_id: "toolu_ask".into(),
        tool_name: "write_file".into(),
        input: valid_input(),
    });

    assert_eq!(denied, ToolResult::error("Permission denied."));
    assert_eq!(denied_counters.checks.get(), 1);
    assert_eq!(denied_counters.validations.get(), 0);
    assert_eq!(denied_counters.executions.get(), 0);

    let bypass_tool = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"));
    let bypass_counters = bypass_tool.counters.clone();
    let mut bypass_registry = ToolRegistry::new();
    bypass_registry.register(Box::new(bypass_tool));
    let bypass_executor = RegistryToolExecutor::new(bypass_registry).with_permission_context(
        context_with_rules(PermissionMode::BypassPermissions, &[], &[], &[]),
    );

    let allowed = bypass_executor.execute(ToolCallRequest {
        tool_use_id: "toolu_bypass".into(),
        tool_name: "write_file".into(),
        input: valid_input(),
    });

    assert_eq!(allowed, ToolResult::success("executed write_file"));
    assert_eq!(bypass_counters.checks.get(), 1);
    assert_eq!(bypass_counters.validations.get(), 1);
    assert_eq!(bypass_counters.executions.get(), 1);
}

#[test]
fn registry_executor_resolves_ask_permissions_before_validation_or_execution() {
    let ask_tool = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"));
    let denied_counters = ask_tool.counters.clone();
    let mut denied_registry = ToolRegistry::new();
    denied_registry.register(Box::new(ask_tool));
    let denied_calls = Arc::new(Mutex::new(Vec::new()));
    let denied_executor = RegistryToolExecutor::new(denied_registry)
        .with_permission_context(context_with_rules(PermissionMode::Default, &[], &[], &[]))
        .with_permission_resolver({
            let denied_calls = Arc::clone(&denied_calls);
            move |request, permission| {
                denied_calls.lock().unwrap().push((
                    request.tool_use_id.clone(),
                    request.tool_name.clone(),
                    permission.message.clone(),
                ));
                false
            }
        });

    let denied = denied_executor.execute(ToolCallRequest {
        tool_use_id: "toolu_deny".into(),
        tool_name: "write_file".into(),
        input: valid_input(),
    });

    assert_eq!(denied, ToolResult::error("Permission denied."));
    assert_eq!(
        denied_calls.lock().unwrap().as_slice(),
        &[(
            "toolu_deny".to_owned(),
            "write_file".to_owned(),
            "Allow Write?".to_owned()
        )]
    );
    assert_eq!(denied_counters.checks.get(), 1);
    assert_eq!(denied_counters.validations.get(), 0);
    assert_eq!(denied_counters.executions.get(), 0);

    let allow_tool = PermissionTool::new("write_file", PermissionResult::ask("Allow Write?"));
    let allowed_counters = allow_tool.counters.clone();
    let mut allowed_registry = ToolRegistry::new();
    allowed_registry.register(Box::new(allow_tool));
    let allowed_calls = Arc::new(Mutex::new(Vec::new()));
    let allowed_executor = RegistryToolExecutor::new(allowed_registry)
        .with_permission_context(context_with_rules(PermissionMode::Default, &[], &[], &[]))
        .with_permission_resolver({
            let allowed_calls = Arc::clone(&allowed_calls);
            move |request, permission| {
                allowed_calls.lock().unwrap().push((
                    request.tool_use_id.clone(),
                    request.tool_name.clone(),
                    permission.message.clone(),
                ));
                true
            }
        });

    let allowed = allowed_executor.execute(ToolCallRequest {
        tool_use_id: "toolu_allow".into(),
        tool_name: "write_file".into(),
        input: valid_input(),
    });

    assert_eq!(allowed, ToolResult::success("executed write_file"));
    assert_eq!(
        allowed_calls.lock().unwrap().as_slice(),
        &[(
            "toolu_allow".to_owned(),
            "write_file".to_owned(),
            "Allow Write?".to_owned()
        )]
    );
    assert_eq!(allowed_counters.checks.get(), 1);
    assert_eq!(allowed_counters.validations.get(), 1);
    assert_eq!(allowed_counters.executions.get(), 1);
}

fn context_with_rules(
    mode: PermissionMode,
    deny_rules: &[(&str, &str)],
    ask_rules: &[(&str, &str)],
    allow_rules: &[(&str, &str)],
) -> ToolPermissionContext {
    ToolPermissionContext {
        mode,
        cwd: "/workspace".into(),
        deny_rules: grouped_rules(deny_rules),
        ask_rules: grouped_rules(ask_rules),
        allow_rules: grouped_rules(allow_rules),
        additional_directories: Vec::new(),
        trusted_read_directories: Vec::new(),
    }
}

fn grouped_rules(entries: &[(&str, &str)]) -> BTreeMap<String, Vec<String>> {
    let mut grouped = BTreeMap::new();
    for (source, rule) in entries {
        grouped
            .entry((*source).to_owned())
            .or_insert_with(Vec::new)
            .push((*rule).to_owned());
    }
    grouped
}

struct PermissionTool {
    name: String,
    result: PermissionResult,
    supports_blanket_allow: bool,
    user_facing_name: String,
    checks: Cell<u32>,
    counters: Rc<PermissionToolCounters>,
}

#[derive(Default)]
struct PermissionToolCounters {
    checks: Cell<u32>,
    validations: Cell<u32>,
    executions: Cell<u32>,
}

impl PermissionTool {
    fn new(name: &str, result: PermissionResult) -> Self {
        Self {
            name: name.into(),
            result,
            supports_blanket_allow: true,
            user_facing_name: name.into(),
            checks: Cell::new(0),
            counters: Rc::new(PermissionToolCounters::default()),
        }
    }

    fn without_blanket_allow(mut self) -> Self {
        self.supports_blanket_allow = false;
        self
    }

    fn with_user_facing_name(mut self, name: &str) -> Self {
        self.user_facing_name = name.into();
        self
    }
}

impl Tool for PermissionTool {
    fn name(&self) -> &str {
        &self.name
    }

    fn description(&self) -> &str {
        "permission test tool"
    }

    fn input_schema(&self) -> JsonValue {
        json::object([("type", json::string("object"))])
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        self.counters
            .validations
            .set(self.counters.validations.get() + 1);
        match input {
            JsonValue::Object(entries) if entries.contains_key("path") => Ok(()),
            _ => Err("missing required field 'path'".into()),
        }
    }

    fn execute(&self, _input: &JsonValue, _context: &ToolContext) -> ToolResult {
        self.counters
            .executions
            .set(self.counters.executions.get() + 1);
        ToolResult::success(format!("executed {}", self.name))
    }

    fn check_permissions(
        &self,
        _input: &JsonValue,
        _context: &ToolPermissionContext,
    ) -> PermissionResult {
        self.checks.set(self.checks.get() + 1);
        self.counters.checks.set(self.counters.checks.get() + 1);
        self.result.clone()
    }

    fn supports_blanket_allow(&self) -> bool {
        self.supports_blanket_allow
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        self.user_facing_name.clone()
    }
}

fn empty_object() -> JsonValue {
    json::object(Vec::<(&str, JsonValue)>::new())
}

fn valid_input() -> JsonValue {
    json::object([("path", json::string("main.tf"))])
}
