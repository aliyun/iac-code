use std::io;
use std::os::fd::RawFd;

use iac_code_tui::RawInputCapture;

use crate::cli_i18n::tr;
use crate::raw_picker::{
    clear_raw_picker, raw_picker_clear_sequence, raw_picker_fit_line_to_width,
    raw_picker_insertable_text, raw_picker_terminal_width, write_raw_interactive_fd_all,
};

#[cfg(unix)]
fn raw_auth_push_title(output: &mut String, title: &str, width: usize) {
    output.push_str("\r\n");
    output.push_str("  \x1b[1m");
    output.push_str(&raw_picker_fit_line_to_width(
        title,
        width.saturating_sub(2),
    ));
    output.push_str("\x1b[0m\r\n\r\n");
}

#[cfg(unix)]
fn raw_auth_push_dim_line(output: &mut String, text: &str, width: usize) {
    output.push_str("  \x1b[38;2;128;128;128m");
    output.push_str(&raw_picker_fit_line_to_width(text, width.saturating_sub(2)));
    output.push_str("\x1b[0m");
}

#[cfg(unix)]
fn raw_auth_masked_existing_hints() -> String {
    format!(
        "Enter {}  Backspace {}  Esc {}",
        tr("Keep"),
        tr("Re-enter"),
        tr("Back")
    )
}

#[cfg(unix)]
fn raw_auth_input_hints() -> String {
    format!("Enter {}  Esc {}", tr("Confirm"), tr("Back"))
}

#[cfg(unix)]
pub(super) fn read_raw_auth_text_input(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    label: &str,
    initial_value: &str,
) -> io::Result<Option<String>> {
    let mut value = initial_value.to_owned();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_auth_text_input(fd, rendered_lines, title, label, &value)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(Some(value));
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "backspace" {
            value.pop();
            rendered_lines = render_raw_auth_text_input(fd, rendered_lines, title, label, &value)?;
            continue;
        }
        if raw_picker_insertable_text(&event) {
            value.push_str(&event.char_text);
            rendered_lines = render_raw_auth_text_input(fd, rendered_lines, title, label, &value)?;
        }
    }
}

#[cfg(unix)]
fn render_raw_auth_text_input(
    fd: RawFd,
    previous_lines: usize,
    title: &str,
    label: &str,
    value: &str,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) =
        raw_auth_text_input_render_output(previous_lines, title, label, value, width);
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
fn raw_auth_text_input_render_output(
    previous_lines: usize,
    title: &str,
    label: &str,
    value: &str,
    width: usize,
) -> (String, usize) {
    let mut output = raw_picker_clear_sequence(previous_lines);
    raw_auth_push_title(&mut output, title, width);
    output.push_str("  ");
    output.push_str(&raw_picker_fit_line_to_width(
        &format!("{label}{value}"),
        width.saturating_sub(2),
    ));
    output.push_str("\x1b[s\r\n\r\n");
    raw_auth_push_dim_line(&mut output, &raw_auth_input_hints(), width);
    output.push_str("\x1b[u");
    (output, 6)
}

#[cfg(unix)]
pub(super) fn read_raw_auth_masked_input(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    label: &str,
    existing: &str,
) -> io::Result<Option<String>> {
    let mut value = String::new();
    let mut keep_existing_mask = !existing.is_empty();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_auth_masked_input(
        fd,
        rendered_lines,
        title,
        label,
        if keep_existing_mask { existing } else { &value },
        keep_existing_mask,
    )?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            clear_raw_picker(fd, rendered_lines)?;
            if keep_existing_mask {
                return Ok(Some(existing.to_owned()));
            }
            return Ok(Some(value));
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "backspace" {
            if keep_existing_mask {
                keep_existing_mask = false;
            } else {
                value.pop();
            }
            rendered_lines = render_raw_auth_masked_input(
                fd,
                rendered_lines,
                title,
                label,
                if keep_existing_mask { existing } else { &value },
                keep_existing_mask,
            )?;
            continue;
        }
        if raw_picker_insertable_text(&event) {
            if keep_existing_mask {
                keep_existing_mask = false;
                value.clear();
            }
            value.push_str(&event.char_text);
            rendered_lines = render_raw_auth_masked_input(
                fd,
                rendered_lines,
                title,
                label,
                if keep_existing_mask { existing } else { &value },
                keep_existing_mask,
            )?;
        }
    }
}

#[cfg(unix)]
fn render_raw_auth_masked_input(
    fd: RawFd,
    previous_lines: usize,
    title: &str,
    label: &str,
    value: &str,
    keep_existing_mask: bool,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) = raw_auth_masked_input_render_output(
        previous_lines,
        title,
        label,
        value,
        keep_existing_mask,
        width,
    );
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn raw_auth_masked_input_render_output(
    previous_lines: usize,
    title: &str,
    label: &str,
    value: &str,
    keep_existing_mask: bool,
    width: usize,
) -> (String, usize) {
    let mut output = raw_picker_clear_sequence(previous_lines);
    raw_auth_push_title(&mut output, title, width);
    output.push_str("  ");
    output.push_str(&raw_picker_fit_line_to_width(
        &format!("{label}{}", "*".repeat(value.len())),
        width.saturating_sub(2),
    ));
    output.push_str("\x1b[s\r\n\r\n");
    let hints = if keep_existing_mask {
        raw_auth_masked_existing_hints()
    } else {
        raw_auth_input_hints()
    };
    raw_auth_push_dim_line(&mut output, &hints, width);
    output.push_str("\x1b[u");
    (output, 6)
}
