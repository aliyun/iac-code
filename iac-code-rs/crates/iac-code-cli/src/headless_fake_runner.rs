use std::env;

use iac_code_config::paths::ConfigPaths;
use iac_code_core::SessionStorage;
use iac_code_exec::{HeadlessRunResult, OutputFormat};
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::StreamEvent;
use iac_code_providers::fake::{FakeProvider, FakeScenario};
use iac_code_tools::SkillManager;

use crate::cli_args::Cli;
use crate::headless_executor::{build_headless_tool_executor, HeadlessToolExecutorOptions};
use crate::headless_usage::persist_headless_usage;
use crate::session_utils::{
    current_git_branch, current_working_directory, resolve_headless_session,
};

pub(super) fn is_fake_provider_enabled() -> bool {
    env::var("IAC_CODE_RS_FAKE_PROVIDER").ok().as_deref() == Some("1")
}

pub(super) fn run_fake_headless(
    prompt: &str,
    output_format: OutputFormat,
    max_turns: u32,
) -> HeadlessRunResult {
    let runner = iac_code_exec::HeadlessRunner::new(
        FakeProvider::new(fake_scenario_from_env()),
        output_format,
        max_turns,
    );
    runner.run(prompt)
}

pub(super) fn run_fake_prompt_from_cli_with_content(
    cli: &Cli,
    prompt: &str,
    prompt_content: Option<AgentMessageContent>,
    output_format: OutputFormat,
    resume: &str,
    continue_session: bool,
    event_sink: Option<&mut dyn FnMut(&StreamEvent)>,
) -> Result<HeadlessRunResult, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let session_storage =
        SessionStorage::new(paths.subdirs().projects).map_err(|error| error.to_string())?;
    let session = resolve_headless_session(&session_storage, &cwd, resume, continue_session)?;
    let provider = FakeProvider::new(fake_scenario_from_env());
    let runner = iac_code_exec::HeadlessRunner::new(provider.clone(), output_format, cli.max_turns)
        .with_initial_conversation(Conversation {
            messages: session.resume_messages.clone(),
        });
    let content = prompt_content.unwrap_or_else(|| AgentMessageContent::Text(prompt.to_owned()));
    let result = if provider.requires_tool_executor() {
        let tool_executor = build_headless_tool_executor(HeadlessToolExecutorOptions {
            paths: &paths,
            allowed_tools: &cli.allowed_tools,
            disallowed_tools: &cli.disallowed_tools,
            permission_mode: &cli.permission_mode,
            cwd: &cwd,
            skill_manager: SkillManager::default(),
            sub_agent_runner: None,
            include_agent_tool: false,
            agent_definition: None,
            auto_approve_permissions: true,
            shared_task_manager: None,
            permission_resolver: None,
            session_id: Some(&session.session_id),
            aliyun_credential_override: None,
        })?;
        if let Some(event_sink) = event_sink {
            runner.run_content_with_tool_executor_and_sink(content, tool_executor, event_sink)
        } else {
            runner.run_content_with_tool_executor(content, tool_executor)
        }
    } else if let Some(event_sink) = event_sink {
        runner.run_content_with_tool_executor_and_sink(
            content,
            iac_code_tools::NoToolExecutor,
            event_sink,
        )
    } else {
        runner.run_content(content)
    };
    let git_branch = current_git_branch(&cwd);
    session_storage
        .save(
            &session.storage_cwd,
            &session.session_id,
            &result.conversation.messages,
            git_branch.as_deref(),
        )
        .map_err(|error| error.to_string())?;
    persist_headless_usage(
        &paths,
        &session.storage_cwd,
        &session.session_id,
        &result,
        "fake",
        "fake",
    );
    Ok(result)
}

pub(super) fn fake_scenario_from_env() -> FakeScenario {
    match env::var("IAC_CODE_RS_FAKE_SCENARIO").ok().as_deref() {
        Some("max_turns") => FakeScenario::MaxTurns,
        Some("conversation_length") => FakeScenario::ConversationLength,
        Some("write_file_auto_approve") => FakeScenario::WriteFileAutoApprove,
        _ => FakeScenario::Text,
    }
}
