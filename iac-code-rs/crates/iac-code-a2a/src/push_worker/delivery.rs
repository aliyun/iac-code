use crate::push_queue::A2APushRetryPolicy;

use super::A2APushDeliveryError;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum FailureClass {
    Transient,
    Permanent,
}

impl FailureClass {
    pub(super) fn is_transient(self) -> bool {
        matches!(self, Self::Transient)
    }
}

pub(super) fn classify_failure(error: &A2APushDeliveryError) -> FailureClass {
    if matches!(
        error,
        A2APushDeliveryError::Timeout(_) | A2APushDeliveryError::Transport(_)
    ) || matches!(
        error,
        A2APushDeliveryError::HttpStatus(408 | 409 | 425 | 429 | 500 | 502 | 503 | 504)
    ) {
        FailureClass::Transient
    } else {
        FailureClass::Permanent
    }
}

pub(super) fn should_retry(
    failure_class: FailureClass,
    next_attempt: u32,
    retry_policy: &A2APushRetryPolicy,
) -> bool {
    failure_class.is_transient() && next_attempt < retry_policy.max_attempts
}
