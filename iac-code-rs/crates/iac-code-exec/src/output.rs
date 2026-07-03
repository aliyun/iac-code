mod events;
mod json_format;
mod render;
mod tools;

use iac_code_protocol::StreamEvent;

pub use events::{OutputCapture, OutputFormat};

pub fn write_events(output_format: OutputFormat, events: &[StreamEvent]) -> OutputCapture {
    render::write_events(output_format, events)
}

pub fn write_progress(events: &[StreamEvent]) -> String {
    render::write_progress(events)
}
