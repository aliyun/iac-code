use crate::ProviderConfig;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenAiCompatibleProvider {
    pub(super) config: ProviderConfig,
}

impl OpenAiCompatibleProvider {
    pub fn new(config: ProviderConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &ProviderConfig {
        &self.config
    }
}
