use iac_code_acp::session::{
    AcpAgent, CompactResult as AcpCompactResult, CompactStatus, MemoryEntry, PermissionDecision,
    RenameOutcome,
};
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::DEFAULT_MODEL;
use iac_code_core::{context_window_config, normalize_session_name, AgentLoop};
use iac_code_exec::OutputFormat;
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::permission::PermissionResult;
use iac_code_protocol::{ErrorEvent, PermissionRequestEvent, StreamEvent, ToJsonValue};
use iac_code_tools::{Memory, MemoryManager, PermissionResolution, ToolCallRequest};

use crate::headless_fake_runner::{fake_scenario_from_env, is_fake_provider_enabled};
use crate::headless_runner::{run_configured_headless, ConfiguredHeadlessOptions};

pub(super) struct AcpHeadlessAgent {
    pub(super) session_id: String,
    pub(super) cwd: String,
    pub(super) conversation: Conversation,
    pub(super) context_usage_percent: f64,
    pub(super) title: Option<String>,
}

impl AcpHeadlessAgent {
    pub(super) fn new(
        session_id: String,
        cwd: String,
        conversation: Conversation,
        title: Option<String>,
    ) -> Self {
        Self {
            session_id,
            cwd,
            conversation,
            context_usage_percent: 0.0,
            title,
        }
    }
}

impl AcpAgent for AcpHeadlessAgent {
    fn run_streaming(
        &mut self,
        prompt: &str,
        request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        self.run_streaming_content(
            AgentMessageContent::Text(prompt.to_owned()),
            prompt,
            request_permission,
        )
    }

    fn run_streaming_content(
        &mut self,
        content: AgentMessageContent,
        prompt_text: &str,
        request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        let permission_callback = std::cell::RefCell::new(request_permission);
        let permission_resolver =
            |request: &ToolCallRequest, permission: &PermissionResult| -> PermissionResolution {
                let event = permission_request_event_from_tool_call(request, permission);
                let mut callback = permission_callback.borrow_mut();
                match (*callback)(event) {
                    PermissionDecision::Allow => PermissionResolution::Allow,
                    PermissionDecision::Deny => PermissionResolution::Deny,
                    PermissionDecision::Cancel => PermissionResolution::Cancel,
                }
            };
        let result = if is_fake_provider_enabled() {
            let runner = iac_code_exec::HeadlessRunner::new(
                iac_code_providers::fake::FakeProvider::new(fake_scenario_from_env()),
                OutputFormat::Text,
                100,
            )
            .with_initial_conversation(self.conversation.clone());
            Ok(runner.run_content(content))
        } else {
            run_configured_headless(ConfiguredHeadlessOptions {
                prompt: prompt_text,
                prompt_content: Some(content),
                cli_model: "",
                output_format: OutputFormat::Text,
                max_turns: 100,
                allowed_tools: "",
                disallowed_tools: "",
                permission_mode: "",
                resume: "",
                continue_session: false,
                verbose: false,
                cwd_override: Some(&self.cwd),
                initial_conversation: Some(self.conversation.clone()),
                session_id_override: Some(&self.session_id),
                persist_session: false,
                shared_task_manager: None,
                auto_approve_permissions: false,
                permission_resolver: Some(&permission_resolver),
                aliyun_credential_override: None,
            })
        };

        match result {
            Ok(result) => {
                self.conversation = result.conversation;
                result.events
            }
            Err(error) => vec![StreamEvent::Error(ErrorEvent {
                error,
                is_retryable: false,
            })],
        }
    }

    fn reset(&mut self) -> Result<(), String> {
        self.conversation = Conversation::default();
        Ok(())
    }

    fn memory_entries(&self) -> Option<Vec<MemoryEntry>> {
        let paths = ConfigPaths::from_env().ok()?;
        let manager = MemoryManager::new(paths.subdirs().memory).ok()?;
        let memories = manager.list_memories().ok()?;
        Some(memories.into_iter().map(acp_memory_entry).collect())
    }

    fn load_memory(&self, name: &str) -> Result<Option<MemoryEntry>, String> {
        let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
        let manager =
            MemoryManager::new(paths.subdirs().memory).map_err(|error| error.to_string())?;
        manager
            .load(name)
            .map(|memory| memory.map(acp_memory_entry))
    }

    fn search_memories(&self, query: &str) -> Result<Vec<MemoryEntry>, String> {
        let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
        let manager =
            MemoryManager::new(paths.subdirs().memory).map_err(|error| error.to_string())?;
        manager
            .search(query)
            .map(|memories| memories.into_iter().map(acp_memory_entry).collect())
    }

    fn delete_memory(&mut self, name: &str) -> Result<bool, String> {
        let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
        let manager =
            MemoryManager::new(paths.subdirs().memory).map_err(|error| error.to_string())?;
        let exists = manager.load(name)?.is_some();
        if !exists {
            return Ok(false);
        }
        manager.delete(name)?;
        Ok(true)
    }

    fn compact(&mut self) -> Result<AcpCompactResult, String> {
        let (result, model) = if is_fake_provider_enabled() {
            let mut agent_loop = AgentLoop::new(
                iac_code_providers::fake::FakeProvider::new(fake_scenario_from_env()),
                1,
            );
            agent_loop.set_model(DEFAULT_MODEL);
            agent_loop.set_conversation(self.conversation.clone());
            let result = agent_loop.compact();
            self.conversation = agent_loop.conversation().clone();
            (result, DEFAULT_MODEL.to_owned())
        } else {
            let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
            let (provider, model) = crate::provider_config::load_configured_provider(&paths, "")?;
            let mut agent_loop = AgentLoop::new(provider, 1);
            agent_loop.set_model(model.clone());
            agent_loop.set_conversation(self.conversation.clone());
            let result = agent_loop.compact();
            self.conversation = agent_loop.conversation().clone();
            (result, model)
        };
        self.context_usage_percent = result.compacted_tokens as f64
            / context_window_config(&model).context_window as f64
            * 100.0;
        Ok(AcpCompactResult {
            status: acp_compact_status(&result.status),
            original_tokens: result.original_tokens,
            compacted_tokens: result.compacted_tokens,
            preserve_recent_turns: result.preserve_recent_turns as u64,
        })
    }

    fn context_usage_percent(&self) -> f64 {
        self.context_usage_percent
    }

    fn rename_session(&mut self, name: &str) -> Result<RenameOutcome, String> {
        let name = normalize_session_name(name)?;
        if self.title.as_deref() == Some(name.as_str()) {
            return Ok(RenameOutcome::Unchanged);
        }
        self.title = Some(name);
        Ok(RenameOutcome::Renamed)
    }
}

fn acp_memory_entry(memory: Memory) -> MemoryEntry {
    MemoryEntry {
        name: memory.name,
        memory_type: memory.memory_type,
        description: memory.description,
        content: memory.content,
    }
}

fn acp_compact_status(status: &str) -> CompactStatus {
    match status {
        "empty" => CompactStatus::Empty,
        "too_short" => CompactStatus::TooShort,
        "too_small" => CompactStatus::TooSmall,
        "success" => CompactStatus::Success,
        _ => CompactStatus::Failed,
    }
}

fn permission_request_event_from_tool_call(
    request: &ToolCallRequest,
    permission: &PermissionResult,
) -> PermissionRequestEvent {
    PermissionRequestEvent {
        tool_name: request.tool_name.clone(),
        tool_input: request.input.clone(),
        tool_use_id: request.tool_use_id.clone(),
        permission_result: Some(permission.to_json_value()),
    }
}
