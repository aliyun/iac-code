mod builder;
mod model;

pub use crate::exposure::A2AExposureType;
pub use builder::{agent_card_to_client_dict, build_agent_card};
pub use model::{AgentCardOptions, AgentExtensionConfig, AgentInterfaceConfig};

pub const IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI: &str = "urn:iac-code:a2a:artifact-metadata:v1";
pub const IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI: &str = "urn:iac-code:a2a:thinking-exposure:v1";

const VERSION: &str = "0.4.1";
