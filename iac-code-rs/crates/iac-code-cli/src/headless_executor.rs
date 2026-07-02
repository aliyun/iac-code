use std::sync::Arc;

use iac_code_config::cloud_credentials::{load_aliyun_credentials, AliyunCredential};
use iac_code_config::paths::ConfigPaths;
use iac_code_exec::OutputFormat;
use iac_code_protocol::permission::PermissionResult;
use iac_code_providers::ConfiguredProvider;
use iac_code_tools::{
    get_agent_definition, register_cloud_tools_with_cloud_credentials_path, register_file_tools,
    register_memory_tools, register_skill_tools, register_task_tools, AgentDefinition,
    AgentProgress, AgentTool, PermissionResolution, RegistryToolExecutor, SkillManager, SkillTool,
    SubAgentRequest, SubAgentResult, SubAgentRunner, TaskManager, ToolCallRequest, ToolContext,
    ToolRegistry,
};

use crate::headless_subagent::{
    count_tool_result_blocks, sub_agent_error_detail, sub_agent_tool_events_from_child_events,
    truncate_sub_agent_output,
};
use crate::permission_settings::{load_tool_permission_context, session_trusted_read_directories};

pub(super) type HeadlessPermissionResolver<'a> =
    dyn Fn(&ToolCallRequest, &PermissionResult) -> PermissionResolution + 'a;

pub(super) struct HeadlessToolExecutorOptions<'a> {
    pub(super) paths: &'a ConfigPaths,
    pub(super) allowed_tools: &'a str,
    pub(super) disallowed_tools: &'a str,
    pub(super) permission_mode: &'a str,
    pub(super) cwd: &'a str,
    pub(super) skill_manager: SkillManager,
    pub(super) sub_agent_runner: Option<Arc<dyn SubAgentRunner>>,
    pub(super) include_agent_tool: bool,
    pub(super) agent_definition: Option<&'a AgentDefinition>,
    pub(super) auto_approve_permissions: bool,
    pub(super) shared_task_manager: Option<TaskManager>,
    pub(super) permission_resolver: Option<&'a HeadlessPermissionResolver<'a>>,
    pub(super) session_id: Option<&'a str>,
    pub(super) aliyun_credential_override: Option<AliyunCredential>,
}

pub(super) fn build_headless_tool_executor<'a>(
    options: HeadlessToolExecutorOptions<'a>,
) -> Result<RegistryToolExecutor<'a>, String> {
    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);
    let aliyun_credential = match options.aliyun_credential_override {
        Some(credential) => Some(credential),
        None => load_aliyun_credentials(options.paths, None).map_err(|error| error.to_string())?,
    };
    register_cloud_tools_with_cloud_credentials_path(
        &mut registry,
        aliyun_credential,
        Some(options.paths.cloud_credentials_path.clone()),
    );
    register_memory_tools(&mut registry, options.paths.subdirs().memory)
        .map_err(|error| error.to_string())?;
    let task_manager = options.shared_task_manager.unwrap_or_default();
    register_task_tools(&mut registry, task_manager.clone());
    if let Some(runner) = options.sub_agent_runner {
        if options.include_agent_tool {
            registry.register(Box::new(
                AgentTool::new(runner.clone()).with_task_manager(task_manager),
            ));
        }
        registry.register(Box::new(
            SkillTool::new(options.skill_manager).with_sub_agent_runner(runner),
        ));
    } else {
        register_skill_tools(&mut registry, options.skill_manager);
    }
    if let Some(definition) = options.agent_definition {
        filter_registry_for_agent_definition(&mut registry, definition);
    }

    let mut permission_context = load_tool_permission_context(
        options.paths,
        options.allowed_tools,
        options.disallowed_tools,
        options.permission_mode,
        options.cwd,
    )?;
    permission_context
        .trusted_read_directories
        .extend(session_trusted_read_directories(
            options.paths,
            options.session_id,
        )?);

    let executor = RegistryToolExecutor::new(registry)
        .with_context(ToolContext {
            cwd: options.cwd.to_owned(),
        })
        .with_permission_context(permission_context)
        .with_auto_approve_permissions(options.auto_approve_permissions);

    Ok(
        if let Some(permission_resolver) = options.permission_resolver {
            executor.with_permission_resolver(move |request, permission| {
                permission_resolver(request, permission)
            })
        } else {
            executor
        },
    )
}

fn filter_registry_for_agent_definition(registry: &mut ToolRegistry, definition: &AgentDefinition) {
    for tool_name in registry.list_tool_names() {
        if !definition.is_tool_allowed(&tool_name) {
            registry.unregister(&tool_name);
        }
    }
}

#[derive(Clone)]
pub(super) struct HeadlessSubAgentRunner {
    pub(super) provider: ConfiguredProvider,
    pub(super) system_prompt: String,
    pub(super) paths: ConfigPaths,
    pub(super) session_id: String,
    pub(super) allowed_tools: String,
    pub(super) disallowed_tools: String,
    pub(super) permission_mode: String,
    pub(super) skill_manager: SkillManager,
    pub(super) aliyun_credential_override: Option<AliyunCredential>,
}

impl SubAgentRunner for HeadlessSubAgentRunner {
    fn run(&self, request: SubAgentRequest) -> Result<SubAgentResult, String> {
        let definition = get_agent_definition(&request.agent_type)
            .ok_or_else(|| format!("Unknown agent type: '{}'", request.agent_type))?;
        let runner = iac_code_exec::HeadlessRunner::new(
            self.provider.clone(),
            OutputFormat::Text,
            definition.max_turns,
        )
        .with_model(self.provider.config().model.clone())
        .with_system_prompt(self.system_prompt.clone())
        .with_result_storage_dir(self.paths.subdirs().tool_results.join(&self.session_id));
        let child_runner: Arc<dyn SubAgentRunner> = Arc::new(self.clone());
        let tool_executor = build_headless_tool_executor(HeadlessToolExecutorOptions {
            paths: &self.paths,
            allowed_tools: &self.allowed_tools,
            disallowed_tools: &self.disallowed_tools,
            permission_mode: &self.permission_mode,
            cwd: &request.cwd,
            skill_manager: self.skill_manager.clone(),
            sub_agent_runner: Some(child_runner),
            include_agent_tool: false,
            agent_definition: Some(&definition),
            auto_approve_permissions: false,
            shared_task_manager: None,
            permission_resolver: None,
            session_id: Some(&self.session_id),
            aliyun_credential_override: self.aliyun_credential_override.clone(),
        })?;
        let result = runner.run_with_tool_executor(&request.prompt, tool_executor);
        if let Some(detail) = sub_agent_error_detail(&result) {
            return Err(detail);
        }
        let stream_events = sub_agent_tool_events_from_child_events(&result.events);
        Ok(SubAgentResult {
            output: truncate_sub_agent_output(result.stdout.trim_end_matches('\n')),
            progress: AgentProgress {
                tool_use_count: count_tool_result_blocks(&result.conversation),
                token_count: result.token_count.min(u32::MAX as u64) as u32,
            },
            stream_events,
        })
    }
}
