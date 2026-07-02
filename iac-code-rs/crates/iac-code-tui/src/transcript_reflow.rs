use std::time::{Duration, Instant};

pub const TRANSCRIPT_REFLOW_DEBOUNCE: Duration = Duration::from_millis(75);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TranscriptWidthObservation {
    pub initialized: bool,
    pub changed: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TranscriptReflowState {
    observed_width: Option<usize>,
    reflowed_width: Option<usize>,
    pending_target_width: Option<usize>,
    pending_deadline: Option<Instant>,
    reflowed_during_stream: bool,
    resize_requested_during_stream: bool,
}

impl TranscriptReflowState {
    pub fn clear(&mut self) {
        *self = Self::default();
    }

    pub fn observe_width(&mut self, width: usize) -> TranscriptWidthObservation {
        let previous = self.observed_width.replace(width);
        if previous.is_none() {
            self.reflowed_width = Some(width);
        }

        TranscriptWidthObservation {
            initialized: previous.is_none(),
            changed: previous.is_some_and(|previous| previous != width),
        }
    }

    pub fn reflow_needed_for_width(&self, width: usize) -> bool {
        self.reflowed_width != Some(width) && self.pending_target_width != Some(width)
    }

    pub fn schedule_debounced_reflow(
        &mut self,
        target_width: Option<usize>,
        now: Instant,
    ) -> Instant {
        if let Some(width) = target_width {
            self.pending_target_width = Some(width);
        }
        let deadline = now + TRANSCRIPT_REFLOW_DEBOUNCE;
        self.pending_deadline = Some(deadline);
        deadline
    }

    pub fn schedule_immediate_reflow(&mut self, now: Instant) {
        self.pending_deadline = Some(now);
        self.pending_target_width = None;
    }

    pub fn pending_deadline(&self) -> Option<Instant> {
        self.pending_deadline
    }

    pub fn pending_target_width(&self) -> Option<usize> {
        self.pending_target_width
    }

    pub fn pending_is_due(&self, now: Instant) -> bool {
        self.pending_deadline
            .is_some_and(|deadline| now >= deadline)
    }

    pub fn has_pending_reflow(&self) -> bool {
        self.pending_deadline.is_some()
    }

    pub fn clear_pending_reflow(&mut self) {
        self.pending_deadline = None;
        self.pending_target_width = None;
    }

    pub fn mark_reflowed_width(&mut self, width: usize) -> bool {
        self.reflowed_width.replace(width) != Some(width)
    }

    pub fn mark_reflowed_during_stream(&mut self) {
        self.reflowed_during_stream = true;
    }

    pub fn mark_resize_requested_during_stream(&mut self) {
        self.resize_requested_during_stream = true;
    }

    pub fn take_stream_finish_reflow_needed(&mut self) -> bool {
        let needed = self.reflowed_during_stream || self.resize_requested_during_stream;
        self.reflowed_during_stream = false;
        self.resize_requested_during_stream = false;
        needed
    }

    pub fn clear_stream_flags(&mut self) {
        self.reflowed_during_stream = false;
        self.resize_requested_during_stream = false;
    }
}
