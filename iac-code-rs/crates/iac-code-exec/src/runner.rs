use std::path::PathBuf;

use iac_code_core::AgentLoop;
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::{MessageEndEvent, StreamEvent};
use iac_code_providers::EventProvider;
use iac_code_tools::{NoToolExecutor, ToolExecutor};

use crate::output::{write_events, write_progress, OutputCapture, OutputFormat};

pub const EXIT_OK: i32 = 0;
pub const EXIT_ERROR: i32 = 1;
pub const EXIT_MAX_TURNS: i32 = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct HeadlessRunResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub conversation: Conversation,
    pub token_count: u64,
    pub events: Vec<StreamEvent>,
}

#[derive(Clone, Debug)]
pub struct HeadlessRunner<P> {
    provider: P,
    output_format: OutputFormat,
    max_turns: u32,
    model: String,
    system_prompt: String,
    verbose: bool,
    initial_conversation: Conversation,
    result_storage_dir: Option<PathBuf>,
}

impl<P> HeadlessRunner<P>
where
    P: EventProvider + Clone,
{
    pub fn new(provider: P, output_format: OutputFormat, max_turns: u32) -> Self {
        Self {
            provider,
            output_format,
            max_turns,
            model: String::new(),
            system_prompt: String::new(),
            verbose: false,
            initial_conversation: Conversation::default(),
            result_storage_dir: None,
        }
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = model.into();
        self
    }

    pub fn with_system_prompt(mut self, system_prompt: impl Into<String>) -> Self {
        self.system_prompt = system_prompt.into();
        self
    }

    pub fn with_verbose(mut self, verbose: bool) -> Self {
        self.verbose = verbose;
        self
    }

    pub fn with_initial_conversation(mut self, conversation: Conversation) -> Self {
        self.initial_conversation = conversation;
        self
    }

    pub fn with_result_storage_dir(mut self, storage_dir: impl Into<PathBuf>) -> Self {
        self.result_storage_dir = Some(storage_dir.into());
        self
    }

    pub fn run(&self, prompt: &str) -> HeadlessRunResult {
        self.run_content(AgentMessageContent::Text(prompt.to_owned()))
    }

    pub fn run_content(&self, content: AgentMessageContent) -> HeadlessRunResult {
        self.run_content_with_tool_executor(content, NoToolExecutor)
    }

    pub fn run_with_tool_executor<T>(&self, prompt: &str, tool_executor: T) -> HeadlessRunResult
    where
        T: ToolExecutor,
    {
        self.run_content_with_tool_executor(
            AgentMessageContent::Text(prompt.to_owned()),
            tool_executor,
        )
    }

    pub fn run_content_with_tool_executor<T>(
        &self,
        content: AgentMessageContent,
        tool_executor: T,
    ) -> HeadlessRunResult
    where
        T: ToolExecutor,
    {
        self.run_content_with_tool_executor_and_sink(content, tool_executor, &mut |_| {})
    }

    pub fn run_content_with_tool_executor_and_sink<T>(
        &self,
        content: AgentMessageContent,
        tool_executor: T,
        event_sink: &mut dyn FnMut(&StreamEvent),
    ) -> HeadlessRunResult
    where
        T: ToolExecutor,
    {
        let mut agent_loop = AgentLoop::with_tool_executor_and_system_prompt(
            self.provider.clone(),
            self.max_turns,
            tool_executor,
            self.system_prompt.clone(),
        );
        agent_loop.set_model(self.model.clone());
        agent_loop.set_conversation(self.initial_conversation.clone());
        if let Some(storage_dir) = &self.result_storage_dir {
            agent_loop = agent_loop.with_result_storage_dir(storage_dir);
        }
        let events = agent_loop.run_streaming_content_with_sink(content, event_sink);
        let conversation = agent_loop.conversation().clone();
        let OutputCapture {
            stdout,
            stderr: output_stderr,
        } = write_events(self.output_format, &events);
        let stderr = if self.verbose {
            let mut progress = write_progress(&events);
            progress.push_str(&output_stderr);
            progress
        } else {
            output_stderr
        };
        HeadlessRunResult {
            exit_code: exit_code_for_events(&events),
            stdout,
            stderr,
            conversation,
            token_count: token_count_for_events(&events),
            events,
        }
    }
}

fn exit_code_for_events(events: &[StreamEvent]) -> i32 {
    let has_error = events
        .iter()
        .any(|event| matches!(event, StreamEvent::Error(_)));
    if has_error {
        return EXIT_ERROR;
    }

    let hit_max_turns = events.iter().any(|event| {
        matches!(
            event,
            StreamEvent::MessageEnd(MessageEndEvent { stop_reason, .. }) if stop_reason == "max_turns"
        )
    });
    if hit_max_turns {
        return EXIT_MAX_TURNS;
    }

    EXIT_OK
}

fn token_count_for_events(events: &[StreamEvent]) -> u64 {
    events
        .iter()
        .filter_map(|event| match event {
            StreamEvent::MessageEnd(MessageEndEvent { usage, .. }) => Some(usage.total_tokens()),
            _ => None,
        })
        .sum()
}
