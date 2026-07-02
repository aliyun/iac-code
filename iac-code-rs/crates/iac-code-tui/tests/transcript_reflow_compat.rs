use std::time::{Duration, Instant};

use iac_code_tui::{TranscriptReflowState, TRANSCRIPT_REFLOW_DEBOUNCE};

#[test]
fn transcript_reflow_first_width_becomes_render_baseline() {
    let mut state = TranscriptReflowState::default();

    let observed = state.observe_width(80);

    assert!(observed.initialized);
    assert!(!observed.changed);
    assert!(!state.reflow_needed_for_width(80));
    assert!(!state.has_pending_reflow());
}

#[test]
fn transcript_reflow_debounces_width_changes_until_deadline() {
    let mut state = TranscriptReflowState::default();
    let now = Instant::now();
    state.observe_width(80);

    let observed = state.observe_width(40);
    assert!(!observed.initialized);
    assert!(observed.changed);
    assert!(state.reflow_needed_for_width(40));

    let deadline = state.schedule_debounced_reflow(Some(40), now);

    assert_eq!(state.pending_deadline(), Some(deadline));
    assert_eq!(state.pending_target_width(), Some(40));
    assert!(!state.reflow_needed_for_width(40));
    assert!(!state.pending_is_due(now + Duration::from_millis(10)));
    assert!(state.pending_is_due(now + TRANSCRIPT_REFLOW_DEBOUNCE));

    state.clear_pending_reflow();
    assert!(state.mark_reflowed_width(40));
    assert!(!state.reflow_needed_for_width(40));
}

#[test]
fn transcript_reflow_rescheduling_pushes_deadline_out() {
    let mut state = TranscriptReflowState::default();
    let now = Instant::now();

    let first_deadline = state.schedule_debounced_reflow(Some(100), now);
    let second_deadline =
        state.schedule_debounced_reflow(Some(120), now + Duration::from_millis(25));

    assert!(second_deadline > first_deadline);
    assert_eq!(state.pending_target_width(), Some(120));
}

#[test]
fn transcript_reflow_streaming_flags_request_one_final_reflow() {
    let mut state = TranscriptReflowState::default();

    state.mark_reflowed_during_stream();
    assert!(state.take_stream_finish_reflow_needed());
    assert!(!state.take_stream_finish_reflow_needed());

    state.mark_resize_requested_during_stream();
    assert!(state.take_stream_finish_reflow_needed());
    assert!(!state.take_stream_finish_reflow_needed());
}
