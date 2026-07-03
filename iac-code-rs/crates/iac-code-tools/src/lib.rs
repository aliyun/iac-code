mod agent_tool;
mod aliyun_api;
mod aliyun_doc_search;
mod bash;
mod executor;
mod file_tools;
mod memory_tools;
mod path_safety;
mod permissions;
mod registry;
mod ros_stack;
mod ros_stack_instances;
mod skill_tools;
mod task_tools;
mod web_fetch;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::provider::ToolDefinition;

pub use agent_tool::{
    builtin_agent_definitions, get_agent_definition, register_agent_tools, AgentDefinition,
    AgentProgress, AgentTool, SubAgentRequest, SubAgentResult, SubAgentRunner,
};
pub use aliyun_api::AliyunApiTool;
pub use aliyun_doc_search::{
    register_cloud_tools, register_cloud_tools_with_cloud_credentials_path, AliyunDocSearchTool,
};
pub use bash::BashTool;
pub use executor::{PermissionResolution, RegistryToolExecutor, ToolExecutionPartition};
pub use file_tools::{
    register_file_tools, EditFileTool, GlobTool, GrepTool, ListFilesTool, ReadFileTool,
    WriteFileTool,
};
pub use iac_code_protocol::tool::{ToolContextModifier, ToolResult};
pub use memory_tools::{
    register_memory_tools, Memory, MemoryManager, ReadMemoryTool, WriteMemoryTool,
};
pub use permissions::check_tool_permission;
pub use registry::{Tool, ToolContext, ToolRegistry};
pub use ros_stack::RosStackTool;
pub use ros_stack_instances::RosStackInstancesTool;
pub use skill_tools::{
    register_skill_tools, SkillDefinition, SkillInvocation, SkillManager, SkillSource, SkillTool,
};
pub use task_tools::{
    register_task_tools, TaskGetTool, TaskInfo, TaskListTool, TaskManager, TaskStatus, TaskStopTool,
};
pub use web_fetch::WebFetchTool;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolCallRequest {
    pub tool_use_id: String,
    pub tool_name: String,
    pub input: JsonValue,
}

pub trait ToolExecutor {
    fn tool_definitions(&self) -> Vec<ToolDefinition> {
        Vec::new()
    }

    fn execute(&self, request: ToolCallRequest) -> ToolResult;

    fn apply_context_modifier(&mut self, _modifier: &ToolContextModifier) {}

    fn execute_batch(&self, requests: &[ToolCallRequest]) -> Vec<ToolResult> {
        requests
            .iter()
            .map(|request| self.execute(request.clone()))
            .collect()
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct NoToolExecutor;

impl ToolExecutor for NoToolExecutor {
    fn execute(&self, request: ToolCallRequest) -> ToolResult {
        ToolResult::error(format!(
            "tool execution is not configured for {}",
            request.tool_name
        ))
    }
}

pub const CRATE_NAME: &str = "iac-code-tools";
