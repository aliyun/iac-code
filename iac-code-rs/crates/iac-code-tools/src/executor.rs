use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};
use iac_code_protocol::provider::ToolDefinition;

use crate::check_tool_permission;
use crate::{
    ToolCallRequest, ToolContext, ToolContextModifier, ToolExecutor, ToolRegistry, ToolResult,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolExecutionPartition {
    pub concurrent: Vec<ToolCallRequest>,
    pub serial: Vec<ToolCallRequest>,
}

pub struct RegistryToolExecutor<'a> {
    registry: ToolRegistry,
    context: ToolContext,
    permission_context: Option<ToolPermissionContext>,
    auto_approve_permissions: bool,
    permission_resolver: Option<PermissionResolver<'a>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PermissionResolution {
    Allow,
    Deny,
    Cancel,
}

impl From<bool> for PermissionResolution {
    fn from(value: bool) -> Self {
        if value {
            Self::Allow
        } else {
            Self::Deny
        }
    }
}

pub type PermissionResolver<'a> =
    Box<dyn Fn(&ToolCallRequest, &PermissionResult) -> PermissionResolution + 'a>;

impl<'a> RegistryToolExecutor<'a> {
    pub fn new(registry: ToolRegistry) -> Self {
        Self {
            registry,
            context: ToolContext::default(),
            permission_context: None,
            auto_approve_permissions: false,
            permission_resolver: None,
        }
    }

    pub fn with_context(mut self, context: ToolContext) -> Self {
        self.context = context;
        self
    }

    pub fn with_permission_context(mut self, permission_context: ToolPermissionContext) -> Self {
        self.permission_context = Some(permission_context);
        self
    }

    pub fn with_auto_approve_permissions(mut self, auto_approve_permissions: bool) -> Self {
        self.auto_approve_permissions = auto_approve_permissions;
        self
    }

    pub fn with_permission_resolver<F, R>(mut self, resolver: F) -> Self
    where
        F: Fn(&ToolCallRequest, &PermissionResult) -> R + 'a,
        R: Into<PermissionResolution>,
    {
        self.permission_resolver = Some(Box::new(move |request, permission| {
            resolver(request, permission).into()
        }));
        self
    }

    pub fn partition(&self, calls: &[ToolCallRequest]) -> ToolExecutionPartition {
        let mut concurrent = Vec::new();
        let mut serial = Vec::new();

        for call in calls {
            match self.registry.get(&call.tool_name) {
                Some(tool) if tool.is_concurrency_safe(&call.input) => {
                    concurrent.push(call.clone());
                }
                _ => serial.push(call.clone()),
            }
        }

        ToolExecutionPartition { concurrent, serial }
    }

    pub fn execute_batch(&self, calls: &[ToolCallRequest]) -> Vec<ToolResult> {
        calls
            .iter()
            .map(|call| self.execute(call.clone()))
            .collect()
    }

    pub fn apply_context_modifier(&mut self, modifier: &ToolContextModifier) {
        if modifier.allowed_tool_rules.is_empty() {
            return;
        }
        if let Some(permission_context) = &mut self.permission_context {
            permission_context
                .allow_rules
                .entry("skill".to_owned())
                .or_default()
                .extend(modifier.allowed_tool_rules.iter().cloned());
        }
    }
}

impl ToolExecutor for RegistryToolExecutor<'_> {
    fn tool_definitions(&self) -> Vec<ToolDefinition> {
        self.registry.to_tool_definitions()
    }

    fn execute(&self, request: ToolCallRequest) -> ToolResult {
        let Some(tool) = self.registry.get(&request.tool_name) else {
            return ToolResult::error(format!("Unknown tool: {}", request.tool_name));
        };

        if let Some(permission_context) = &self.permission_context {
            let permission = check_tool_permission(tool, &request.input, permission_context);
            match self.resolve_permission(&request, &permission) {
                PermissionResolution::Allow => {}
                PermissionResolution::Deny => return permission_denied_result(&permission),
                PermissionResolution::Cancel => {
                    return ToolResult::cancelled("Tool execution cancelled.");
                }
            }
        }

        if let Err(error) = tool.validate_input(&request.input) {
            return ToolResult::error(format!(
                "Invalid input for tool '{}': {}. Please provide all required parameters as defined in the tool schema.",
                request.tool_name, error
            ));
        }

        tool.execute(&request.input, &self.context)
    }

    fn execute_batch(&self, requests: &[ToolCallRequest]) -> Vec<ToolResult> {
        RegistryToolExecutor::execute_batch(self, requests)
    }

    fn apply_context_modifier(&mut self, modifier: &ToolContextModifier) {
        RegistryToolExecutor::apply_context_modifier(self, modifier);
    }
}

impl RegistryToolExecutor<'_> {
    fn resolve_permission(
        &self,
        request: &ToolCallRequest,
        permission: &PermissionResult,
    ) -> PermissionResolution {
        if permission.behavior == "allow" {
            return PermissionResolution::Allow;
        }

        if permission.behavior != "ask" {
            return PermissionResolution::Deny;
        }

        if self.auto_approve_permissions {
            return PermissionResolution::Allow;
        }

        self.permission_resolver
            .as_ref()
            .map_or(PermissionResolution::Deny, |resolver| {
                resolver(request, permission)
            })
    }
}

fn permission_denied_result(permission: &PermissionResult) -> ToolResult {
    if permission.behavior == "deny" && !permission.message.is_empty() {
        return ToolResult::error(permission.message.clone());
    }

    ToolResult::error("Permission denied.")
}
