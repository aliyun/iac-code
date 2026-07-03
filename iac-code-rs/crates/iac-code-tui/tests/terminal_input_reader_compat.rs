#![cfg(unix)]

use std::os::fd::RawFd;
use std::time::Duration;

use iac_code_tui::{read_terminal_key, PromptKeyEvent, RawTerminalModeGuard};

struct PseudoTerminal {
    master: RawFd,
    slave: RawFd,
}

impl PseudoTerminal {
    fn open() -> Self {
        let mut master = -1;
        let mut slave = -1;
        let status = unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        assert_eq!(
            status,
            0,
            "openpty failed: {}",
            std::io::Error::last_os_error()
        );
        Self { master, slave }
    }
}

impl Drop for PseudoTerminal {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.master);
            libc::close(self.slave);
        }
    }
}

fn write_fd(fd: RawFd, bytes: &[u8]) {
    let mut written = 0;
    while written < bytes.len() {
        let status =
            unsafe { libc::write(fd, bytes[written..].as_ptr().cast(), bytes.len() - written) };
        assert!(
            status >= 0,
            "write failed: {}",
            std::io::Error::last_os_error()
        );
        written += status as usize;
    }
}

#[test]
fn terminal_input_reader_returns_none_when_timeout_expires() {
    let pty = PseudoTerminal::open();
    let _raw = RawTerminalModeGuard::enter(pty.slave).unwrap();

    let event = read_terminal_key(pty.slave, Some(Duration::from_millis(10))).unwrap();

    assert!(event.is_none());
}

#[test]
fn terminal_input_reader_reads_ascii_bytes_like_python_raw_input() {
    let pty = PseudoTerminal::open();
    let _raw = RawTerminalModeGuard::enter(pty.slave).unwrap();
    write_fd(pty.master, b"a");

    let event = read_terminal_key(pty.slave, Some(Duration::from_secs(1)))
        .unwrap()
        .expect("key event");

    assert_key(event, "a", false, false, false);
}

#[test]
fn terminal_input_reader_reads_escape_sequences_like_python_raw_input() {
    let pty = PseudoTerminal::open();
    let _raw = RawTerminalModeGuard::enter(pty.slave).unwrap();
    write_fd(pty.master, b"\x1b[A");

    let event = read_terminal_key(pty.slave, Some(Duration::from_secs(1)))
        .unwrap()
        .expect("key event");

    assert_key(event, "up", false, false, false);
}

#[test]
fn terminal_input_reader_reads_utf8_chars_like_python_raw_input() {
    let pty = PseudoTerminal::open();
    let _raw = RawTerminalModeGuard::enter(pty.slave).unwrap();
    write_fd(pty.master, "你".as_bytes());

    let event = read_terminal_key(pty.slave, Some(Duration::from_secs(1)))
        .unwrap()
        .expect("key event");

    assert_key(event.clone(), "你", false, false, false);
    assert_eq!(event.char_text, "你");
}

#[test]
fn terminal_input_reader_reads_bracketed_paste_like_python_raw_input() {
    let pty = PseudoTerminal::open();
    let _raw = RawTerminalModeGuard::enter(pty.slave).unwrap();
    write_fd(pty.master, b"\x1b[200~hello\r\nworld\x1b[201~");

    let event = read_terminal_key(pty.slave, Some(Duration::from_secs(1)))
        .unwrap()
        .expect("key event");

    assert_key(event.clone(), "paste", false, false, false);
    assert_eq!(event.char_text, "hello\nworld");
}

fn assert_key(event: PromptKeyEvent, key: &str, ctrl: bool, alt: bool, shift: bool) {
    assert_eq!(event.key, key);
    assert_eq!(event.ctrl, ctrl);
    assert_eq!(event.alt, alt);
    assert_eq!(event.shift, shift);
}
