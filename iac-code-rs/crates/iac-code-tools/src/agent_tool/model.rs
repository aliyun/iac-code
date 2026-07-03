use iac_code_protocol::SubAgentToolEvent;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentDefinition {
    pub agent_type: String,
    pub when_to_use: String,
    pub tools: Option<Vec<String>>,
    pub disallowed_tools: Vec<String>,
    pub max_turns: u32,
    pub model: String,
}

impl AgentDefinition {
    pub fn allows_all_tools(&self) -> bool {
        self.tools
            .as_ref()
            .is_some_and(|tools| tools.iter().any(|tool| tool == "*"))
    }

    pub fn is_tool_allowed(&self, tool_name: &str) -> bool {
        if self
            .disallowed_tools
            .iter()
            .any(|disallowed| disallowed == tool_name)
        {
            return false;
        }
        if self.allows_all_tools() {
            return true;
        }
        self.tools
            .as_ref()
            .is_some_and(|tools| tools.iter().any(|tool| tool == tool_name))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentProgress {
    pub tool_use_count: u32,
    pub token_count: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SubAgentRequest {
    pub prompt: String,
    pub agent_type: String,
    pub cwd: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SubAgentResult {
    pub output: String,
    pub progress: AgentProgress,
    pub stream_events: Vec<SubAgentToolEvent>,
}

pub trait SubAgentRunner: Send + Sync + 'static {
    fn run(&self, request: SubAgentRequest) -> Result<SubAgentResult, String>;
}
