mod output;
mod runner;

pub use output::OutputFormat;
pub use runner::{HeadlessRunResult, HeadlessRunner, EXIT_ERROR, EXIT_MAX_TURNS, EXIT_OK};

pub const CRATE_NAME: &str = "iac-code-exec";
