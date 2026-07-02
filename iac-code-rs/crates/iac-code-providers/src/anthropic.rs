mod config;
mod errors;
mod event;
mod payload;
mod request;
mod response;
mod sse;
mod usage;

use crate::ProviderConfig;
pub use errors::StreamChatError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnthropicProvider {
    config: ProviderConfig,
}

impl AnthropicProvider {
    pub fn new(config: ProviderConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &ProviderConfig {
        &self.config
    }
}
