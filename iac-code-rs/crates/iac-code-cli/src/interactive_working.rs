use std::io::{self, IsTerminal, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use iac_code_exec::OutputFormat;

use crate::ansi::{ANSI_DIM, ANSI_ORANGE, ANSI_RESET};
use crate::cli_i18n::tr;

pub(super) fn start_interactive_working_indicator(
    output_format: OutputFormat,
) -> Option<InteractiveWorkingIndicator> {
    start_interactive_working_indicator_with_status(output_format, tr("Working"))
}

pub(super) fn start_interactive_working_indicator_with_status(
    output_format: OutputFormat,
    status: String,
) -> Option<InteractiveWorkingIndicator> {
    if output_format != OutputFormat::Text || !io::stdout().is_terminal() {
        return None;
    }
    Some(InteractiveWorkingIndicator::start(status))
}

pub(super) struct InteractiveWorkingIndicator {
    stop: Arc<AtomicBool>,
    state: Arc<Mutex<WorkingIndicatorState>>,
    status: String,
    started: Instant,
    handle: Option<thread::JoinHandle<()>>,
}

/// Live-status bookkeeping shared between the spinner thread and the main
/// render loop.
///
/// The spinner paints its status line at the bottom of the live region. The
/// first frame after a (re)start prints a leading blank line to separate the
/// spinner from the content above; later frames repaint that same line in
/// place. The blank line therefore stays on screen across those inline
/// repaints, so anyone clearing the live region (`pause_and_clear`,
/// `stop_and_clear`) must step the cursor back up over it — otherwise the
/// renderer's own cursor-up clears land one row too low and orphan the
/// previous live-thinking snapshot.
#[derive(Default)]
pub(super) struct WorkingIndicatorState {
    paused: bool,
    has_painted: bool,
    pub(super) needs_leading_blank: bool,
    leading_blank_on_screen: bool,
    frame_index: usize,
}

impl WorkingIndicatorState {
    /// Decide whether the next spinner frame opens a fresh block with a
    /// leading blank line, recording that the blank is now on screen.
    pub(super) fn next_frame_uses_leading_blank(&mut self) -> bool {
        let uses = !self.has_painted || self.needs_leading_blank;
        self.has_painted = true;
        self.needs_leading_blank = false;
        if uses {
            self.leading_blank_on_screen = true;
        }
        uses
    }

    /// Consume the on-screen leading blank when clearing the live region,
    /// returning whether the cursor must step back up over it.
    pub(super) fn take_leading_blank(&mut self) -> bool {
        std::mem::take(&mut self.leading_blank_on_screen)
    }

    /// Render the next spinner frame and advance the animation. Used by both
    /// the spinner thread and `resume`, so the animation never resets.
    pub(super) fn paint_next_frame(&mut self, status: &str, elapsed: Duration) -> String {
        let frame = INTERACTIVE_SPINNER_FRAMES[self.frame_index % INTERACTIVE_SPINNER_FRAMES.len()];
        self.frame_index = self.frame_index.wrapping_add(1);
        if self.next_frame_uses_leading_blank() {
            interactive_working_frame(status, frame, elapsed)
        } else {
            interactive_working_frame_inline(status, frame, elapsed)
        }
    }
}

const INTERACTIVE_SPINNER_FRAMES: &[&str] = &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

fn lock_working_indicator_state(
    state: &Mutex<WorkingIndicatorState>,
) -> std::sync::MutexGuard<'_, WorkingIndicatorState> {
    state
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl InteractiveWorkingIndicator {
    pub(super) fn start(status: String) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let state = Arc::new(Mutex::new(WorkingIndicatorState::default()));
        let started = Instant::now();
        let thread_stop = Arc::clone(&stop);
        let thread_state = Arc::clone(&state);
        let thread_status = status.clone();
        let handle = thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                {
                    // Hold the lock across the paint so a concurrent
                    // `pause_and_clear` can never interleave a stale frame
                    // after it has cleared the live region.
                    let mut state = lock_working_indicator_state(&thread_state);
                    if !state.paused {
                        let output = state.paint_next_frame(&thread_status, started.elapsed());
                        print!("{output}");
                        let _ = io::stdout().flush();
                    }
                }
                thread::sleep(Duration::from_millis(80));
            }
        });
        Self {
            stop,
            state,
            status,
            started,
            handle: Some(handle),
        }
    }

    pub(super) fn pause_and_clear(&self) {
        let mut state = lock_working_indicator_state(&self.state);
        state.paused = true;
        let used_leading_blank = state.take_leading_blank();
        print!(
            "{}",
            interactive_working_pause_clear_sequence(used_leading_blank)
        );
        let _ = io::stdout().flush();
    }

    pub(super) fn resume(&self) {
        // Repaint the status line immediately so it never vanishes in the gap
        // between a content update and the spinner thread's next tick — that
        // gap is what made the indicator flicker badly while reasoning streamed.
        // We just cleared the region, so reopen it with a leading blank line.
        let mut state = lock_working_indicator_state(&self.state);
        state.paused = false;
        state.needs_leading_blank = true;
        let output = state.paint_next_frame(&self.status, self.started.elapsed());
        print!("{output}");
        let _ = io::stdout().flush();
    }

    pub(super) fn stop_and_clear(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
        let mut state = lock_working_indicator_state(&self.state);
        let used_leading_blank = state.take_leading_blank();
        print!(
            "{}",
            interactive_working_pause_clear_sequence(used_leading_blank)
        );
        let _ = io::stdout().flush();
    }
}

impl Drop for InteractiveWorkingIndicator {
    fn drop(&mut self) {
        self.stop_and_clear();
    }
}

pub(super) fn interactive_working_pause_clear_sequence(used_leading_blank: bool) -> String {
    let mut output = "\r\x1b[2K".to_owned();
    if used_leading_blank {
        output.push_str("\x1b[1A\r");
    }
    output
}

pub(super) fn format_spinner_elapsed(elapsed: Duration) -> String {
    let seconds = elapsed.as_secs();
    if seconds < 60 {
        format!("{seconds}s")
    } else {
        format!("{}m {:02}s", seconds / 60, seconds % 60)
    }
}

pub(super) fn interactive_working_frame(status: &str, frame: &str, elapsed: Duration) -> String {
    format_interactive_working_frame(status, frame, elapsed, true)
}

pub(super) fn interactive_working_frame_inline(
    status: &str,
    frame: &str,
    elapsed: Duration,
) -> String {
    format_interactive_working_frame(status, frame, elapsed, false)
}

fn format_interactive_working_frame(
    status: &str,
    frame: &str,
    elapsed: Duration,
    leading_blank_line: bool,
) -> String {
    let elapsed = format_spinner_elapsed(elapsed);
    let leading = if leading_blank_line { "\n" } else { "" };
    format!(
        "{leading}\r\x1b[2K{ANSI_ORANGE}{frame} {status}...{ANSI_RESET} {ANSI_DIM}({elapsed}){ANSI_RESET}"
    )
}
