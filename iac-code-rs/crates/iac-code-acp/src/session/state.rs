use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::ToolPermissionContext;

use crate::state::TurnState;

use super::mcp_config::AcpMcpServerConfig;
use super::model::AcpAgent;
use super::permission::PermissionCache;

const DEFAULT_PERMISSION_CACHE_MAX_SIZE: usize = 128;

pub struct AcpSession<A> {
    pub(super) id: String,
    pub(super) agent: A,
    pub(super) current_turn: Option<TurnState>,
    pub(super) last_active: Instant,
    pub(super) permission_cache: PermissionCache,
    pub(super) permission_context: Option<ToolPermissionContext>,
    pub(super) blanket_allow_disabled_tools: BTreeSet<String>,
    pub(super) terminal_tool_names: BTreeSet<String>,
    mcp_configs: Vec<AcpMcpServerConfig>,
    dynamic_config: BTreeMap<String, JsonValue>,
    closed: bool,
}

impl<A> AcpSession<A>
where
    A: AcpAgent,
{
    pub fn new(session_id: impl Into<String>, agent: A) -> Self {
        Self {
            id: session_id.into(),
            agent,
            current_turn: None,
            last_active: Instant::now(),
            permission_cache: PermissionCache::new(DEFAULT_PERMISSION_CACHE_MAX_SIZE),
            permission_context: None,
            blanket_allow_disabled_tools: BTreeSet::new(),
            terminal_tool_names: BTreeSet::new(),
            mcp_configs: Vec::new(),
            dynamic_config: BTreeMap::new(),
            closed: false,
        }
    }

    pub fn with_mcp_configs(mut self, configs: Vec<AcpMcpServerConfig>) -> Self {
        self.mcp_configs = configs;
        self
    }

    pub fn agent(&self) -> &A {
        &self.agent
    }

    pub fn agent_mut(&mut self) -> &mut A {
        &mut self.agent
    }

    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn current_turn_id(&self) -> Option<&str> {
        self.current_turn.as_ref().map(|turn| turn.turn_id.as_str())
    }

    pub fn last_active(&self) -> Instant {
        self.last_active
    }

    pub fn is_closed(&self) -> bool {
        self.closed
    }

    pub fn permission_context(&self) -> Option<&ToolPermissionContext> {
        self.permission_context.as_ref()
    }

    pub fn mcp_configs(&self) -> &[AcpMcpServerConfig] {
        &self.mcp_configs
    }

    pub fn update_config(&mut self, config: BTreeMap<String, JsonValue>) {
        self.dynamic_config.extend(config);
    }

    pub fn config(&self) -> BTreeMap<String, JsonValue> {
        self.dynamic_config.clone()
    }

    pub fn set_permission_context(&mut self, context: Option<ToolPermissionContext>) {
        self.permission_context = context;
    }

    pub fn disable_blanket_allow(&mut self, tool_name: impl Into<String>) {
        self.blanket_allow_disabled_tools.insert(tool_name.into());
    }

    pub fn set_terminal_tools<I, S>(&mut self, names: I)
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.terminal_tool_names = names.into_iter().map(Into::into).collect();
    }

    pub fn set_permission_cache_max_size(&mut self, max_size: usize) {
        self.permission_cache.set_max_size(max_size);
    }

    pub fn cache_permission(&mut self, tool_name: impl Into<String>, decision: impl Into<String>) {
        self.permission_cache
            .record(tool_name.into(), decision.into());
    }

    pub fn permission_cache_snapshot(&self) -> Vec<(String, String)> {
        self.permission_cache.snapshot()
    }

    pub fn close(&mut self) {
        if self.closed {
            return;
        }
        self.current_turn = None;
        self.permission_cache.clear();
        self.permission_context = None;
        self.closed = true;
    }

    pub fn touch(&mut self) {
        self.last_active = Instant::now();
    }

    pub(super) fn is_open(&self) -> bool {
        !self.closed
    }
}
