use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::StreamEvent;

use crate::{AnthropicProvider, EventProvider, OpenAiCompatibleProvider, ProviderConfig};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfiguredProvider {
    OpenAiCompatible(OpenAiCompatibleProvider),
    Anthropic(AnthropicProvider),
}

impl ConfiguredProvider {
    pub fn new(config: ProviderConfig) -> Self {
        if uses_anthropic_protocol(&config.provider_key) {
            Self::Anthropic(AnthropicProvider::new(config))
        } else {
            Self::OpenAiCompatible(OpenAiCompatibleProvider::new(config))
        }
    }

    pub fn config(&self) -> &ProviderConfig {
        match self {
            Self::OpenAiCompatible(provider) => provider.config(),
            Self::Anthropic(provider) => provider.config(),
        }
    }
}

impl EventProvider for ConfiguredProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_turns: u32,
    ) -> Vec<StreamEvent> {
        match self {
            Self::OpenAiCompatible(provider) => {
                provider.stream_events(conversation, system, tools, max_turns)
            }
            Self::Anthropic(provider) => {
                provider.stream_events(conversation, system, tools, max_turns)
            }
        }
    }

    fn stream_events_with_sink(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_turns: u32,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Vec<StreamEvent> {
        match self {
            Self::OpenAiCompatible(provider) => {
                provider.stream_events_with_sink(conversation, system, tools, max_turns, sink)
            }
            Self::Anthropic(provider) => {
                provider.stream_events_with_sink(conversation, system, tools, max_turns, sink)
            }
        }
    }
}

fn uses_anthropic_protocol(provider_key: &str) -> bool {
    matches!(
        provider_key,
        "anthropic" | "anthropic_compatible" | "minimax_cn" | "minimax_intl"
    )
}
