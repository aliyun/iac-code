use iac_code_tui::{
    format_spinner_elapsed, spinner_frame_at, ShimmerSpinnerState, COMPLETION_VERBS, SPINNER_DOTS,
    SPINNER_VERBS,
};

#[test]
fn spinner_elapsed_format_matches_python() {
    assert_eq!(format_spinner_elapsed(0.0), "0s");
    assert_eq!(format_spinner_elapsed(1.0), "1s");
    assert_eq!(format_spinner_elapsed(59.0), "59s");
    assert_eq!(format_spinner_elapsed(59.9), "60s");
    assert_eq!(format_spinner_elapsed(60.0), "1m 0s");
    assert_eq!(format_spinner_elapsed(65.0), "1m 5s");
    assert_eq!(format_spinner_elapsed(3661.0), "61m 1s");
}

#[test]
fn spinner_constants_match_python_sequences() {
    assert_eq!(
        SPINNER_DOTS,
        ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    );
    assert_eq!(SPINNER_VERBS, ["Processing", "Working"]);
    assert_eq!(COMPLETION_VERBS, ["Thought", "Processed", "Worked"]);
}

#[test]
fn spinner_frame_uses_wall_clock_interval_like_python() {
    assert_eq!(spinner_frame_at(0.00), "⠋");
    assert_eq!(spinner_frame_at(0.08), "⠙");
    assert_eq!(spinner_frame_at(0.16), "⠹");
    assert_eq!(spinner_frame_at(0.80), "⠋");
}

#[test]
fn shimmer_spinner_state_defaults_customizes_elapsed_and_updates_status() {
    let mut spinner = ShimmerSpinnerState::new(None, 10.0);
    assert_eq!(spinner.status(), "Processing...");
    assert_eq!(spinner.elapsed(12.5), 2.5);
    assert_eq!(spinner.render_plain(12.5), "⠦ Processing... (2s)");

    spinner.update_status("Deploying...");
    assert_eq!(spinner.status(), "Deploying...");
    assert_eq!(spinner.start_time_seconds(), 10.0);
    assert!(spinner.render_plain(70.0).contains("Deploying... (1m 0s)"));

    let custom = ShimmerSpinnerState::new(Some("Testing"), 1.0);
    assert_eq!(custom.status(), "Testing");
    assert_eq!(custom.render_plain(1.0), "⠹ Testing (0s)");
}
