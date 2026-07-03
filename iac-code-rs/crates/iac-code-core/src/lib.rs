mod agent_loop;
mod context;
mod result_storage;
mod session;
mod session_usage;
mod system_prompt;

pub use agent_loop::AgentLoop;
pub use context::{
    context_window_config, ContextManager, ContextUsage, ContextWindowConfig, TokenBudget,
    TokenCounter,
};
pub use result_storage::ResultStorage;
pub use session::{
    normalize_session_name, read_session_metadata, sanitize_path, validate_session_name,
    write_session_metadata, SessionEntry, SessionIndex, SessionMetadata, SessionStorage,
    SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME, SESSION_METADATA_SCHEMA_VERSION,
    SESSION_NAME_PATTERN_TEXT,
};
pub use session_usage::{SessionUsageStore, SessionUsageTotals, USAGE_JSONL_FILENAME};
pub use system_prompt::{build_system_prompt, DYNAMIC_BOUNDARY};

pub const CRATE_NAME: &str = "iac-code-core";
