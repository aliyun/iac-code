mod anthropic;
mod configured;
pub mod fake;
mod manager;
mod multimodal;
mod openai_compatible;
mod qwenpaw;
mod registry;
mod tool_input_parser;

use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::StreamEvent;

pub use anthropic::AnthropicProvider;
pub use configured::ConfiguredProvider;
pub use manager::{create_provider_config, ProviderConfig};
pub use multimodal::{
    builtin_multimodal_models, get_multimodal_spec, is_model_multimodal,
    is_model_multimodal_with_probe, probe_openapi_compatible, AutoDetectCache, MultiModalSpec,
    MultimodalProbeOptions, DEFAULT_FORMATS,
};
pub use openai_compatible::OpenAiCompatibleProvider;
pub use qwenpaw::{
    is_qwenpaw_available, load_from_qwenpaw, qwenpaw_provider_mappings, QwenPawConfig,
};
pub use registry::{provider_descriptor, provider_keys, ModelEntry, ProviderDescriptor};

pub const CRATE_NAME: &str = "iac-code-providers";

pub trait EventProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_turns: u32,
    ) -> Vec<StreamEvent>;

    fn stream_events_with_sink(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_turns: u32,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Vec<StreamEvent> {
        let events = self.stream_events(conversation, system, tools, max_turns);
        for event in &events {
            sink(event);
        }
        events
    }
}
