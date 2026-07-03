mod mcp_config;
mod model;
mod permission;
mod prompt;
mod slash;
mod state;
mod updates;

pub use mcp_config::{convert_mcp_server_configs, AcpMcpServerConfig};
pub use model::{
    AcpAgent, AcpClient, AcpError, CompactResult, CompactStatus, MemoryEntry, PermissionDecision,
    PromptResponse, RenameOutcome,
};
pub use state::AcpSession;
