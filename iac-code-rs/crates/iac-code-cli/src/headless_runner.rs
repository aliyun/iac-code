use std::sync::Arc;

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::load_disabled_skills;
use iac_code_core::{build_system_prompt, SessionStorage};
use iac_code_exec::{HeadlessRunResult, OutputFormat};
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::StreamEvent;
use iac_code_tools::{MemoryManager, SkillManager, TaskManager, ToolContext};

use crate::cli_args::Cli;
use crate::headless_executor::{
    build_headless_tool_executor, HeadlessPermissionResolver, HeadlessSubAgentRunner,
    HeadlessToolExecutorOptions,
};
use crate::headless_fake_runner::{
    is_fake_provider_enabled, run_fake_headless, run_fake_prompt_from_cli_with_content,
};
use crate::headless_usage::persist_headless_usage;
use crate::prompt_content::{ensure_prompt_content_supported, local_image_path_prompt_content};
use crate::provider_config::load_configured_provider;
use crate::session_utils::{
    current_git_branch, current_working_directory, new_session_id, resolve_headless_session,
    HeadlessSession,
};

pub(super) struct ConfiguredHeadlessOptions<'a> {
    pub(super) prompt: &'a str,
    pub(super) prompt_content: Option<AgentMessageContent>,
    pub(super) cli_model: &'a str,
    pub(super) output_format: OutputFormat,
    pub(super) max_turns: u32,
    pub(super) allowed_tools: &'a str,
    pub(super) disallowed_tools: &'a str,
    pub(super) permission_mode: &'a str,
    pub(super) resume: &'a str,
    pub(super) continue_session: bool,
    pub(super) verbose: bool,
    pub(super) cwd_override: Option<&'a str>,
    pub(super) initial_conversation: Option<Conversation>,
    pub(super) session_id_override: Option<&'a str>,
    pub(super) persist_session: bool,
    pub(super) shared_task_manager: Option<TaskManager>,
    pub(super) auto_approve_permissions: bool,
    pub(super) permission_resolver: Option<&'a HeadlessPermissionResolver<'a>>,
    pub(super) aliyun_credential_override: Option<AliyunCredential>,
}

pub(super) struct RunPromptWithSinkOptions<'a> {
    pub(super) cli: &'a Cli,
    pub(super) prompt: &'a str,
    pub(super) prompt_content: Option<AgentMessageContent>,
    pub(super) output_format: OutputFormat,
    pub(super) resume: &'a str,
    pub(super) continue_session: bool,
    pub(super) shared_task_manager: Option<TaskManager>,
    pub(super) event_sink: Option<&'a mut dyn FnMut(&StreamEvent)>,
}

pub(super) fn run_prompt_from_cli(
    cli: &Cli,
    prompt: &str,
    output_format: OutputFormat,
    resume: &str,
    continue_session: bool,
    shared_task_manager: Option<TaskManager>,
) -> Result<HeadlessRunResult, String> {
    run_prompt_from_cli_with_content(
        cli,
        prompt,
        None,
        output_format,
        resume,
        continue_session,
        shared_task_manager,
    )
}

pub(super) fn run_prompt_from_cli_with_content(
    cli: &Cli,
    prompt: &str,
    prompt_content: Option<AgentMessageContent>,
    output_format: OutputFormat,
    resume: &str,
    continue_session: bool,
    shared_task_manager: Option<TaskManager>,
) -> Result<HeadlessRunResult, String> {
    run_prompt_from_cli_with_content_and_sink(RunPromptWithSinkOptions {
        cli,
        prompt,
        prompt_content,
        output_format,
        resume,
        continue_session,
        shared_task_manager,
        event_sink: None,
    })
}

pub(super) fn run_prompt_from_cli_with_content_and_sink(
    options: RunPromptWithSinkOptions<'_>,
) -> Result<HeadlessRunResult, String> {
    let RunPromptWithSinkOptions {
        cli,
        prompt,
        prompt_content,
        output_format,
        resume,
        continue_session,
        shared_task_manager,
        event_sink,
    } = options;
    if is_fake_provider_enabled() {
        return run_fake_prompt_from_cli_with_content(
            cli,
            prompt,
            prompt_content,
            output_format,
            resume,
            continue_session,
            event_sink,
        );
    }
    run_configured_headless_with_sink(
        ConfiguredHeadlessOptions {
            prompt,
            prompt_content,
            cli_model: &cli.model,
            output_format,
            max_turns: cli.max_turns,
            allowed_tools: &cli.allowed_tools,
            disallowed_tools: &cli.disallowed_tools,
            permission_mode: &cli.permission_mode,
            resume,
            continue_session,
            verbose: cli.verbose,
            cwd_override: None,
            initial_conversation: None,
            session_id_override: None,
            persist_session: true,
            shared_task_manager,
            auto_approve_permissions: true,
            permission_resolver: None,
            aliyun_credential_override: None,
        },
        event_sink,
    )
}

pub(super) fn write_headless_result(result: &HeadlessRunResult) {
    print!("{}", result.stdout);
    eprint!("{}", result.stderr);
}

pub(super) fn run_a2a_server_headless(
    prompt: &str,
    cwd: &str,
    cli_model: Option<&str>,
    aliyun_credential_override: Option<AliyunCredential>,
    auto_approve_permissions: bool,
) -> Result<HeadlessRunResult, String> {
    if is_fake_provider_enabled() {
        return Ok(run_fake_headless(prompt, OutputFormat::Text, 100));
    }
    run_configured_headless(ConfiguredHeadlessOptions {
        prompt,
        prompt_content: None,
        cli_model: cli_model.unwrap_or(""),
        output_format: OutputFormat::Text,
        max_turns: 100,
        allowed_tools: "",
        disallowed_tools: "",
        permission_mode: "",
        resume: "",
        continue_session: false,
        verbose: false,
        cwd_override: Some(cwd),
        initial_conversation: None,
        session_id_override: None,
        persist_session: true,
        shared_task_manager: None,
        auto_approve_permissions,
        permission_resolver: None,
        aliyun_credential_override,
    })
}

pub(super) fn a2a_headless_error_message(result: &HeadlessRunResult) -> String {
    let stderr = result.stderr.trim();
    if !stderr.is_empty() {
        return stderr.to_owned();
    }
    let stdout = result.stdout.trim();
    if !stdout.is_empty() {
        return stdout.to_owned();
    }
    "Agent execution failed.".to_owned()
}

pub(super) fn run_configured_headless(
    options: ConfiguredHeadlessOptions<'_>,
) -> Result<HeadlessRunResult, String> {
    run_configured_headless_with_sink(options, None)
}

fn run_configured_headless_with_sink(
    options: ConfiguredHeadlessOptions<'_>,
    event_sink: Option<&mut dyn FnMut(&StreamEvent)>,
) -> Result<HeadlessRunResult, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let (provider, model) = load_configured_provider(&paths, options.cli_model)?;

    let cwd = options
        .cwd_override
        .map(ToOwned::to_owned)
        .map(Ok)
        .unwrap_or_else(current_working_directory)?;
    let session_storage =
        SessionStorage::new(paths.subdirs().projects).map_err(|error| error.to_string())?;
    let session = if let Some(conversation) = &options.initial_conversation {
        HeadlessSession {
            session_id: options
                .session_id_override
                .map(ToOwned::to_owned)
                .unwrap_or_else(new_session_id),
            storage_cwd: cwd.clone(),
            resume_messages: conversation.messages.clone(),
        }
    } else {
        resolve_headless_session(
            &session_storage,
            &cwd,
            options.resume,
            options.continue_session,
        )?
    };
    let memory_content = MemoryManager::new(paths.subdirs().memory)
        .map(|manager| manager.get_prompt_content())
        .map_err(|error| error.to_string())?;
    let discovered_skill_manager = SkillManager::discover(paths.subdirs().skills, &cwd)?;
    let disabled_skill_names = load_disabled_skills(&paths).map_err(|error| error.to_string())?;
    let skill_manager = discovered_skill_manager.enabled_only(&disabled_skill_names);
    let skill_listing = skill_manager.build_listing();
    let system_prompt = build_system_prompt(&cwd, &memory_content, &skill_listing);
    let sub_agent_runner = Arc::new(HeadlessSubAgentRunner {
        provider: provider.clone(),
        system_prompt: system_prompt.clone(),
        paths: paths.clone(),
        session_id: session.session_id.clone(),
        allowed_tools: options.allowed_tools.to_owned(),
        disallowed_tools: options.disallowed_tools.to_owned(),
        permission_mode: options.permission_mode.to_owned(),
        skill_manager: skill_manager.clone(),
        aliyun_credential_override: options.aliyun_credential_override.clone(),
    });
    let mut initial_messages = session.resume_messages;
    let auto_trigger_context = ToolContext { cwd: cwd.clone() };
    let auto_triggered_messages = skill_manager.auto_triggered_messages(
        options.prompt,
        &auto_trigger_context,
        &initial_messages,
    );
    initial_messages.extend(auto_triggered_messages);
    let provider_config = provider.config().clone();
    let prompt_content = match options.prompt_content {
        Some(content) => content,
        None => local_image_path_prompt_content(options.prompt)?
            .unwrap_or_else(|| AgentMessageContent::Text(options.prompt.to_owned())),
    };
    ensure_prompt_content_supported(&prompt_content, &paths, &provider_config, &model)?;
    let runner =
        iac_code_exec::HeadlessRunner::new(provider, options.output_format, options.max_turns)
            .with_model(model)
            .with_system_prompt(system_prompt)
            .with_initial_conversation(Conversation {
                messages: initial_messages,
            })
            .with_verbose(options.verbose)
            .with_result_storage_dir(paths.subdirs().tool_results.join(&session.session_id));
    let tool_executor = build_headless_tool_executor(HeadlessToolExecutorOptions {
        paths: &paths,
        allowed_tools: options.allowed_tools,
        disallowed_tools: options.disallowed_tools,
        permission_mode: options.permission_mode,
        cwd: &cwd,
        skill_manager,
        sub_agent_runner: Some(sub_agent_runner),
        include_agent_tool: true,
        agent_definition: None,
        auto_approve_permissions: options.auto_approve_permissions,
        shared_task_manager: options.shared_task_manager,
        permission_resolver: options.permission_resolver,
        session_id: Some(&session.session_id),
        aliyun_credential_override: options.aliyun_credential_override,
    })?;
    let result = if let Some(event_sink) = event_sink {
        runner.run_content_with_tool_executor_and_sink(prompt_content, tool_executor, event_sink)
    } else {
        runner.run_content_with_tool_executor(prompt_content, tool_executor)
    };
    let git_branch = current_git_branch(&cwd);
    if options.persist_session {
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
            &provider_config.provider_key,
            &provider_config.model,
        );
    }
    Ok(result)
}
