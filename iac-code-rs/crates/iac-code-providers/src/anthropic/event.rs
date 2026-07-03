use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::{NonStreamingResponse, ToolDefinition};
use iac_code_protocol::{ErrorEvent, StreamEvent};

use crate::{manager::fallback_model, EventProvider};

use super::response::stream_events_from_response;
use super::sse::tombstone_events_for_orphaned_messages;
use super::AnthropicProvider;

impl EventProvider for AnthropicProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        match self.stream_chat(conversation, system, tools, 8192) {
            Ok(events) => events,
            Err(stream_error) => {
                let mut events = stream_error.partial_events;
                let tombstones = tombstone_events_for_orphaned_messages(&events);
                match self.complete_chat(conversation, system, tools, 8192) {
                    Ok(response) => {
                        events.extend(tombstones);
                        events.extend(stream_events_from_response(response));
                        events
                    }
                    Err(primary_complete_error) => {
                        if let Some(response) = self.complete_chat_with_degraded_model(
                            conversation,
                            system,
                            tools,
                            8192,
                        ) {
                            events.extend(tombstones);
                            events.extend(stream_events_from_response(response));
                            return events;
                        }
                        events.extend(tombstones);
                        events.push(StreamEvent::Error(ErrorEvent {
                            error: primary_complete_error_or_stream_error(
                                &stream_error.message,
                                &primary_complete_error,
                            ),
                            is_retryable: false,
                        }));
                        events
                    }
                }
            }
        }
    }

    fn stream_events_with_sink(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        _max_turns: u32,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Vec<StreamEvent> {
        match self.stream_chat_with_sink(conversation, system, tools, 8192, sink) {
            Ok(events) => events,
            Err(stream_error) => {
                let mut events = stream_error.partial_events;
                let tombstones = tombstone_events_for_orphaned_messages(&events);
                match self.complete_chat(conversation, system, tools, 8192) {
                    Ok(response) => {
                        for event in &tombstones {
                            sink(event);
                        }
                        events.extend(tombstones);
                        let fallback_events = stream_events_from_response(response);
                        for event in &fallback_events {
                            sink(event);
                        }
                        events.extend(fallback_events);
                        events
                    }
                    Err(primary_complete_error) => {
                        if let Some(response) = self.complete_chat_with_degraded_model(
                            conversation,
                            system,
                            tools,
                            8192,
                        ) {
                            for event in &tombstones {
                                sink(event);
                            }
                            events.extend(tombstones);
                            let fallback_events = stream_events_from_response(response);
                            for event in &fallback_events {
                                sink(event);
                            }
                            events.extend(fallback_events);
                            return events;
                        }
                        for event in &tombstones {
                            sink(event);
                        }
                        events.extend(tombstones);
                        let event = StreamEvent::Error(ErrorEvent {
                            error: primary_complete_error_or_stream_error(
                                &stream_error.message,
                                &primary_complete_error,
                            ),
                            is_retryable: false,
                        });
                        sink(&event);
                        events.push(event);
                        events
                    }
                }
            }
        }
    }
}

impl AnthropicProvider {
    fn complete_chat_with_degraded_model(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Option<NonStreamingResponse> {
        let fallback = fallback_model(&self.config.model)?;
        let provider = Self::new(self.config.with_model(fallback));
        provider
            .complete_chat(conversation, system, tools, max_tokens)
            .ok()
    }
}

fn primary_complete_error_or_stream_error(
    stream_error: &str,
    primary_complete_error: &str,
) -> String {
    if primary_complete_error.trim().is_empty() {
        stream_error.to_owned()
    } else {
        primary_complete_error.to_owned()
    }
}
