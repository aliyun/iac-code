use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;

use iac_code_core::normalize_session_name;
#[cfg(unix)]
use iac_code_tui::RawInputCapture;

use super::cli_i18n::tr;
#[cfg(unix)]
use super::raw_picker::{
    clear_raw_picker, raw_picker_clear_sequence, raw_picker_query_prompt_line,
    raw_picker_terminal_width, write_raw_interactive_fd_all, RawPickerSearchQuery,
};
#[cfg(unix)]
use super::raw_prompt_renderer::write_raw_interactive_prompt_newline;

#[cfg(unix)]
pub(super) fn read_raw_rename_name_prompt(
    fd: RawFd,
    capture: &RawInputCapture,
) -> io::Result<Option<String>> {
    loop {
        let Some(raw_name) = read_raw_inline_text_prompt(fd, capture, &tr("Session name:"))? else {
            return Ok(None);
        };
        if raw_name.trim().is_empty() {
            write_raw_inline_error(fd, &tr("Session name cannot be empty."))?;
            continue;
        }
        match normalize_session_name(raw_name.trim()) {
            Ok(name) => return Ok(Some(name)),
            Err(error) => {
                write_raw_inline_error(fd, &error.to_string())?;
            }
        }
    }
}

#[cfg(unix)]
fn read_raw_inline_text_prompt(
    fd: RawFd,
    capture: &RawInputCapture,
    label: &str,
) -> io::Result<Option<String>> {
    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_inline_text_prompt(fd, rendered_lines, label, &query)?;
    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            render_raw_inline_text_prompt(fd, rendered_lines, label, &query)?;
            write_raw_interactive_prompt_newline(fd)?;
            return Ok(Some(query.text().to_owned()));
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if query.handle_key(&event) {
            rendered_lines = render_raw_inline_text_prompt(fd, rendered_lines, label, &query)?;
        }
    }
}

#[cfg(unix)]
fn render_raw_inline_text_prompt(
    fd: RawFd,
    previous_lines: usize,
    label: &str,
    query: &RawPickerSearchQuery,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_picker_clear_sequence(previous_lines);
    output.push_str("\r\n\x1b[2K\x1b[1m\x1b[96m");
    output.push_str(&raw_picker_query_prompt_line(
        &format!("{label} "),
        query,
        width,
    ));
    output.push_str("\x1b[0m");
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(1)
}

#[cfg(unix)]
fn write_raw_inline_error(fd: RawFd, message: &str) -> io::Result<()> {
    write_raw_interactive_fd_all(fd, format!("\x1b[31m{message}\x1b[0m\r\n").as_bytes())
}
