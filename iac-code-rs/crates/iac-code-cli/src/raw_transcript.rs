use std::io;
use std::os::fd::RawFd;
use std::time::Duration;

use iac_code_tui::{
    draw_transcript_view_wrapped, terminal_dimensions, transcript_should_exit, RawInputCapture,
    TerminalDimensions,
};

use crate::raw_picker::write_raw_interactive_fd_all;

#[cfg(unix)]
pub(super) fn read_raw_transcript_view(
    fd: RawFd,
    capture: &RawInputCapture,
    lines: &[String],
) -> io::Result<()> {
    let mut screen = RawAlternateScreenGuard::enter(fd)?;
    let mut rendered_dimensions = raw_transcript_dimensions(fd);
    render_raw_transcript_view(fd, lines)?;
    loop {
        if let Some(event) = capture.read_key(Some(Duration::from_millis(100)))? {
            if transcript_should_exit(&event.key, event.ctrl) {
                screen.exit()?;
                return Ok(());
            }
        }

        let dimensions = raw_transcript_dimensions(fd);
        if dimensions != rendered_dimensions {
            render_raw_transcript_view(fd, lines)?;
            rendered_dimensions = dimensions;
        }
    }
}

#[cfg(unix)]
pub(super) struct RawAlternateScreenGuard {
    fd: RawFd,
    active: bool,
}

#[cfg(unix)]
impl RawAlternateScreenGuard {
    pub(super) fn enter(fd: RawFd) -> io::Result<Self> {
        write_raw_interactive_fd_all(fd, b"\x1b[?1049h\x1b[H")?;
        Ok(Self { fd, active: true })
    }

    pub(super) fn exit(&mut self) -> io::Result<()> {
        if self.active {
            write_raw_interactive_fd_all(self.fd, b"\x1b[?1049l")?;
            self.active = false;
        }
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for RawAlternateScreenGuard {
    fn drop(&mut self) {
        let _ = self.exit();
    }
}

#[cfg(unix)]
pub(super) struct RawMouseTrackingGuard {
    fd: RawFd,
    active: bool,
}

#[cfg(unix)]
impl RawMouseTrackingGuard {
    pub(super) fn enter(fd: RawFd) -> io::Result<Self> {
        write_raw_interactive_fd_all(fd, b"\x1b[?1000h\x1b[?1006h")?;
        Ok(Self { fd, active: true })
    }

    pub(super) fn exit(&mut self) -> io::Result<()> {
        if self.active {
            write_raw_interactive_fd_all(self.fd, b"\x1b[?1006l\x1b[?1000l")?;
            self.active = false;
        }
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for RawMouseTrackingGuard {
    fn drop(&mut self) {
        let _ = self.exit();
    }
}

#[cfg(unix)]
pub(super) fn render_raw_transcript_view(fd: RawFd, lines: &[String]) -> io::Result<usize> {
    let dimensions = terminal_dimensions(fd).ok().flatten();
    let rows = dimensions.map(|size| size.rows).unwrap_or(24).max(1);
    let width = dimensions.map(|size| size.columns).unwrap_or(80).max(1);
    let drawn = draw_transcript_view_wrapped(lines, rows, width);
    let content_rows = rows.saturating_sub(2).max(1);
    let mut output = "\x1b[H\x1b[2J".to_owned();
    for line in &drawn.visible_lines {
        output.push_str(line);
        output.push_str("\r\n");
    }
    for _ in drawn.visible_lines.len()..content_rows {
        output.push_str("\r\n");
    }
    output.push_str("\r\n");
    output.push_str(&format!(
        "\x1b[{rows};1H\x1b[2K\x1b[2m{}\x1b[0m",
        drawn.footer
    ));
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(drawn.visible_lines.len() + 1)
}

#[cfg(unix)]
fn raw_transcript_dimensions(fd: RawFd) -> Option<TerminalDimensions> {
    terminal_dimensions(fd).ok().flatten()
}
