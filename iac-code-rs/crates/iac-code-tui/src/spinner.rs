pub const SPINNER_DOTS: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
pub const SPINNER_COLOR: &str = "rgb(215,119,87)";
pub const SPINNER_VERBS: [&str; 2] = ["Processing", "Working"];
pub const COMPLETION_VERBS: [&str; 3] = ["Thought", "Processed", "Worked"];

const FRAME_INTERVAL_SECONDS: f64 = 0.08;

#[derive(Clone, Debug, PartialEq)]
pub struct ShimmerSpinnerState {
    status: String,
    start_time_seconds: f64,
}

impl ShimmerSpinnerState {
    pub fn new(status: Option<&str>, start_time_seconds: f64) -> Self {
        Self {
            status: status
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{}...", SPINNER_VERBS[0])),
            start_time_seconds,
        }
    }

    pub fn status(&self) -> &str {
        &self.status
    }

    pub fn start_time_seconds(&self) -> f64 {
        self.start_time_seconds
    }

    pub fn elapsed(&self, now_seconds: f64) -> f64 {
        now_seconds - self.start_time_seconds
    }

    pub fn render_plain(&self, now_seconds: f64) -> String {
        let elapsed = self.elapsed(now_seconds);
        format!(
            "{} {} ({})",
            spinner_frame_at(now_seconds),
            self.status,
            format_spinner_elapsed(elapsed)
        )
    }

    pub fn update_status(&mut self, status: impl Into<String>) {
        self.status = status.into();
    }
}

pub fn format_spinner_elapsed(seconds: f64) -> String {
    if seconds < 60.0 {
        return format!("{seconds:.0}s");
    }
    let minutes = (seconds / 60.0).floor() as u64;
    let secs = (seconds % 60.0) as u64;
    format!("{minutes}m {secs}s")
}

pub fn spinner_frame_at(monotonic_seconds: f64) -> &'static str {
    let frame_index =
        ((monotonic_seconds / FRAME_INTERVAL_SECONDS) + 1e-9).floor() as usize % SPINNER_DOTS.len();
    SPINNER_DOTS[frame_index]
}
