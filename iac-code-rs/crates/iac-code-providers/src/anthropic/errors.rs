use iac_code_protocol::StreamEvent;

#[derive(Clone, Debug, PartialEq)]
pub struct StreamChatError {
    pub(super) message: String,
    pub(super) partial_events: Vec<StreamEvent>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct CompleteChatError {
    pub(super) message: String,
    pub(super) retryable: bool,
}

impl CompleteChatError {
    pub(super) fn retryable(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: true,
        }
    }

    pub(super) fn non_retryable(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: false,
        }
    }
}

impl StreamChatError {
    pub(super) fn new(message: impl Into<String>, partial_events: Vec<StreamEvent>) -> Self {
        Self {
            message: message.into(),
            partial_events,
        }
    }

    pub(super) fn without_partial(message: impl Into<String>) -> Self {
        Self::new(message, Vec::new())
    }
}
