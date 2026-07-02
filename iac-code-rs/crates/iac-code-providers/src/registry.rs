mod descriptors;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelEntry {
    pub id: String,
    pub is_default: bool,
    pub support_multimodal: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderDescriptor {
    pub key: String,
    pub name: String,
    pub display_name: String,
    pub base_url: Option<String>,
    pub models: Vec<ModelEntry>,
    pub require_api_key: bool,
    pub is_local: bool,
    pub supports_stream_options: bool,
}

impl ProviderDescriptor {
    pub fn default_model(&self) -> String {
        self.models
            .iter()
            .find(|model| model.is_default)
            .or_else(|| self.models.first())
            .map(|model| model.id.clone())
            .unwrap_or_default()
    }

    pub fn model_ids(&self) -> Vec<String> {
        self.models.iter().map(|model| model.id.clone()).collect()
    }
}

pub const PROVIDER_KEYS: &[&str] = descriptors::PROVIDER_KEYS;

pub fn provider_keys() -> &'static [&'static str] {
    PROVIDER_KEYS
}

pub fn provider_descriptor(key: &str) -> Option<ProviderDescriptor> {
    descriptors::provider_descriptor(key)
}
