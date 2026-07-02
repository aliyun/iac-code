use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;

#[cfg(unix)]
use iac_code_tui::RawInputCapture;

use super::cli_i18n::tr;
#[cfg(unix)]
use super::raw_picker::{
    clear_raw_picker, raw_picker_clear_sequence, raw_picker_fit_line_to_width,
    raw_picker_terminal_width, write_raw_interactive_fd_all,
};

#[cfg(unix)]
#[derive(Clone, Debug)]
pub(super) struct RawSelectOption {
    label: String,
    disabled: bool,
}

#[cfg(unix)]
impl RawSelectOption {
    pub(super) fn new(label: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            disabled: false,
        }
    }

    pub(super) fn disabled(label: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            disabled: true,
        }
    }
}

#[cfg(unix)]
pub(super) fn read_raw_select_index(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    options: &[RawSelectOption],
    default_index: usize,
) -> io::Result<Option<usize>> {
    read_raw_select_index_with_info(fd, capture, title, &[], options, default_index)
}

#[cfg(unix)]
pub(super) fn read_raw_select_index_with_info(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    info_lines: &[String],
    options: &[RawSelectOption],
    default_index: usize,
) -> io::Result<Option<usize>> {
    if options.is_empty() {
        return Ok(None);
    }
    let mut focused = raw_select_initial_focus(options, default_index);
    let mut rendered_lines = 0usize;
    rendered_lines =
        render_raw_select_with_info(fd, rendered_lines, title, info_lines, options, focused)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            if options.get(focused).is_some_and(|option| option.disabled) {
                continue;
            }
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(Some(focused));
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            focused = raw_select_move_focus(options, focused, -1);
            rendered_lines = render_raw_select_with_info(
                fd,
                rendered_lines,
                title,
                info_lines,
                options,
                focused,
            )?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            focused = raw_select_move_focus(options, focused, 1);
            rendered_lines = render_raw_select_with_info(
                fd,
                rendered_lines,
                title,
                info_lines,
                options,
                focused,
            )?;
        }
    }
}

#[cfg(unix)]
pub(super) fn raw_select_initial_focus(options: &[RawSelectOption], default_index: usize) -> usize {
    let clamped = default_index.min(options.len().saturating_sub(1));
    if options.get(clamped).is_some_and(|option| !option.disabled) {
        return clamped;
    }
    options
        .iter()
        .position(|option| !option.disabled)
        .unwrap_or(clamped)
}

#[cfg(unix)]
fn raw_select_move_focus(options: &[RawSelectOption], focused: usize, direction: isize) -> usize {
    if options.is_empty() || direction == 0 {
        return focused;
    }
    let step = if direction > 0 { 1 } else { -1 };
    let mut index = focused as isize + step;
    while index >= 0 && (index as usize) < options.len() {
        if !options[index as usize].disabled {
            return index as usize;
        }
        index += step;
    }
    focused
}

#[cfg(unix)]
pub(super) fn render_raw_select_with_info(
    fd: RawFd,
    previous_lines: usize,
    title: &str,
    info_lines: &[String],
    options: &[RawSelectOption],
    focused: usize,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) = raw_select_render_output_with_info(
        previous_lines,
        title,
        info_lines,
        options,
        focused,
        width,
    );
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn raw_select_render_output(
    previous_lines: usize,
    title: &str,
    options: &[RawSelectOption],
    focused: usize,
    width: usize,
) -> (String, usize) {
    raw_select_render_output_with_info(previous_lines, title, &[], options, focused, width)
}

#[cfg(unix)]
fn raw_select_render_output_with_info(
    previous_lines: usize,
    title: &str,
    info_lines: &[String],
    options: &[RawSelectOption],
    focused: usize,
    width: usize,
) -> (String, usize) {
    let mut output = raw_picker_clear_sequence(previous_lines);
    raw_select_push_title(&mut output, title, width);
    if !info_lines.is_empty() {
        for line in info_lines {
            raw_select_push_dim_line(&mut output, line, width);
            output.push_str("\r\n");
        }
        output.push_str("\r\n");
    }
    for (index, option) in options.iter().enumerate() {
        raw_select_push_option(&mut output, option, index == focused, width);
    }
    output.push_str("\r\n");
    raw_select_push_dim_line(&mut output, &raw_select_hints(), width);
    (
        output,
        options.len() + 5 + info_lines.len() + usize::from(!info_lines.is_empty()),
    )
}

#[cfg(unix)]
fn raw_select_push_option(
    output: &mut String,
    option: &RawSelectOption,
    focused: bool,
    width: usize,
) {
    if focused && !option.disabled {
        let plain = format!("> {}", option.label);
        let fitted = raw_picker_fit_line_to_width(&plain, width.saturating_sub(2));
        output.push_str("  \x1b[96m");
        output.push_str(&fitted);
        output.push_str("\x1b[0m\r\n");
        return;
    }

    let fitted = raw_picker_fit_line_to_width(&option.label, width.saturating_sub(4));
    output.push_str("    \x1b[38;2;128;128;128m");
    output.push_str(&fitted);
    output.push_str("\x1b[0m\r\n");
}

#[cfg(unix)]
fn raw_select_push_title(output: &mut String, title: &str, width: usize) {
    output.push_str("\r\n");
    output.push_str("  \x1b[1m");
    output.push_str(&raw_picker_fit_line_to_width(
        title,
        width.saturating_sub(2),
    ));
    output.push_str("\x1b[0m\r\n\r\n");
}

#[cfg(unix)]
fn raw_select_push_dim_line(output: &mut String, text: &str, width: usize) {
    output.push_str("  \x1b[38;2;128;128;128m");
    output.push_str(&raw_picker_fit_line_to_width(text, width.saturating_sub(2)));
    output.push_str("\x1b[0m");
}

#[cfg(unix)]
fn raw_select_hints() -> String {
    format!(
        "↑↓ {}  Enter {}  Esc {}",
        tr("Navigate"),
        tr("Confirm"),
        tr("Back")
    )
}
