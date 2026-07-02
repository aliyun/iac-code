use std::io;
use std::os::fd::RawFd;

use crate::cli_i18n::tr;
use crate::raw_picker::{
    raw_picker_clear_sequence, raw_picker_fit_line_to_width, raw_picker_terminal_width,
    write_raw_interactive_fd_all,
};

pub(super) fn render_raw_auth_aliyun_oauth_waiting(
    fd: RawFd,
    authorization_url: &str,
) -> io::Result<()> {
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_picker_clear_sequence(0);
    raw_auth_oauth_push_title(&mut output, &tr("Waiting for browser authorization"), width);
    for line in [
        tr("1. The browser may show official-cli; this is the Alibaba Cloud official CLI OAuth application."),
        tr("2. After authorization, this terminal will continue automatically."),
        tr("Press Esc to cancel while waiting."),
        String::new(),
        tr("Open in your browser:"),
        authorization_url.to_owned(),
    ] {
        if line.is_empty() {
            output.push_str("\r\n");
        } else {
            raw_auth_oauth_push_dim_line(&mut output, &line, width);
            output.push_str("\r\n");
        }
    }
    write_raw_interactive_fd_all(fd, output.as_bytes())
}

fn raw_auth_oauth_push_title(output: &mut String, title: &str, width: usize) {
    output.push_str("\r\n");
    output.push_str("  \x1b[1m");
    output.push_str(&raw_picker_fit_line_to_width(
        title,
        width.saturating_sub(2),
    ));
    output.push_str("\x1b[0m\r\n\r\n");
}

fn raw_auth_oauth_push_dim_line(output: &mut String, text: &str, width: usize) {
    output.push_str("  \x1b[38;2;128;128;128m");
    output.push_str(&raw_picker_fit_line_to_width(text, width.saturating_sub(2)));
    output.push_str("\x1b[0m");
}
