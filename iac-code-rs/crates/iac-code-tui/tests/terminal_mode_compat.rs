use iac_code_tui::{
    make_raw_termios, terminal_mode_enter_sequences, terminal_mode_exit_sequences, RawInputCapture,
    RawTerminalModeGuard, TerminalModeGuard,
};

#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::Duration;

fn joined(sequences: &[&[u8]]) -> Vec<u8> {
    sequences.concat()
}

#[test]
fn terminal_mode_enter_sequences_match_python_raw_input_capture() {
    assert_eq!(
        terminal_mode_enter_sequences(),
        &[
            b"\x1b[?2004h".as_slice(),
            b"\x1b[?1004h".as_slice(),
            b"\x1b[>1u".as_slice(),
            b"\x1b[>4;2m".as_slice(),
        ]
    );
}

#[test]
fn terminal_mode_exit_sequences_match_python_raw_input_capture() {
    assert_eq!(
        terminal_mode_exit_sequences(),
        &[
            b"\x1b[>4;0m".as_slice(),
            b"\x1b[<u".as_slice(),
            b"\x1b[?1004l".as_slice(),
            b"\x1b[?2004l".as_slice(),
        ]
    );
}

#[test]
fn terminal_mode_guard_writes_enter_then_exit_on_explicit_exit() {
    let mut output = Vec::new();
    {
        let mut guard = TerminalModeGuard::enter(&mut output).unwrap();

        guard.exit().unwrap();
    }

    let mut expected = joined(terminal_mode_enter_sequences());
    expected.extend(joined(terminal_mode_exit_sequences()));
    assert_eq!(output, expected);
}

#[test]
fn terminal_mode_guard_writes_exit_on_drop() {
    let mut output = Vec::new();
    {
        let _guard = TerminalModeGuard::enter(&mut output).unwrap();
    }

    let mut expected = joined(terminal_mode_enter_sequences());
    expected.extend(joined(terminal_mode_exit_sequences()));
    assert_eq!(output, expected);
}

#[test]
fn terminal_mode_guard_exit_is_idempotent() {
    let mut output = Vec::new();
    {
        let mut guard = TerminalModeGuard::enter(&mut output).unwrap();

        guard.exit().unwrap();
        guard.exit().unwrap();
    }

    let mut expected = joined(terminal_mode_enter_sequences());
    expected.extend(joined(terminal_mode_exit_sequences()));
    assert_eq!(output, expected);
}

#[cfg(unix)]
#[test]
fn raw_termios_conversion_matches_python_tty_setraw_flags() {
    let mut termios: libc::termios = unsafe { std::mem::zeroed() };
    termios.c_iflag =
        libc::BRKINT | libc::ICRNL | libc::INPCK | libc::ISTRIP | libc::IXON | libc::IXOFF;
    termios.c_oflag = libc::OPOST | libc::ONLCR;
    termios.c_cflag = libc::CS7 | libc::PARENB | libc::HUPCL;
    termios.c_lflag = libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG | libc::NOFLSH;
    termios.c_cc[libc::VMIN] = 9;
    termios.c_cc[libc::VTIME] = 7;

    let raw = make_raw_termios(termios);

    assert_eq!(
        raw.c_iflag & (libc::BRKINT | libc::ICRNL | libc::INPCK | libc::ISTRIP | libc::IXON),
        0
    );
    assert_eq!(raw.c_iflag & libc::IXOFF, libc::IXOFF);
    assert_eq!(raw.c_oflag & libc::OPOST, 0);
    assert_eq!(raw.c_oflag & libc::ONLCR, libc::ONLCR);
    assert_eq!(raw.c_cflag & libc::PARENB, 0);
    assert_eq!(raw.c_cflag & libc::CSIZE, libc::CS8);
    assert_eq!(raw.c_cflag & libc::HUPCL, libc::HUPCL);
    assert_eq!(
        raw.c_lflag & (libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG),
        0
    );
    assert_eq!(raw.c_lflag & libc::NOFLSH, libc::NOFLSH);
    assert_eq!(raw.c_cc[libc::VMIN], 1);
    assert_eq!(raw.c_cc[libc::VTIME], 0);
}

#[cfg(unix)]
struct PseudoTerminal {
    master: RawFd,
    slave: RawFd,
}

#[cfg(unix)]
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

#[cfg(unix)]
impl Drop for PseudoTerminal {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.master);
            libc::close(self.slave);
        }
    }
}

#[cfg(unix)]
fn current_termios(fd: RawFd) -> libc::termios {
    let mut termios: libc::termios = unsafe { std::mem::zeroed() };
    let status = unsafe { libc::tcgetattr(fd, &mut termios) };
    assert_eq!(
        status,
        0,
        "tcgetattr failed: {}",
        std::io::Error::last_os_error()
    );
    termios
}

#[cfg(unix)]
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

#[cfg(unix)]
fn read_fd_exact(fd: RawFd, len: usize) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(len);
    while bytes.len() < len {
        assert!(wait_fd_readable(fd, Duration::from_secs(1)));
        let remaining = len - bytes.len();
        let mut chunk = vec![0; remaining];
        let status = unsafe { libc::read(fd, chunk.as_mut_ptr().cast(), remaining) };
        assert!(
            status > 0,
            "read failed: {}",
            std::io::Error::last_os_error()
        );
        chunk.truncate(status as usize);
        bytes.extend(chunk);
    }
    bytes
}

#[cfg(unix)]
fn wait_fd_readable(fd: RawFd, timeout: Duration) -> bool {
    let mut readfds: libc::fd_set = unsafe { std::mem::zeroed() };
    unsafe {
        libc::FD_ZERO(&mut readfds);
        libc::FD_SET(fd, &mut readfds);
    }
    let mut timeout = libc::timeval {
        tv_sec: timeout.as_secs() as libc::time_t,
        tv_usec: timeout.subsec_micros() as libc::suseconds_t,
    };
    let status = unsafe {
        libc::select(
            fd + 1,
            &mut readfds,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut timeout,
        )
    };
    assert!(
        status >= 0,
        "select failed: {}",
        std::io::Error::last_os_error()
    );
    status > 0
}

#[cfg(unix)]
fn assert_restored_termios_matches_python_restore(
    actual: &libc::termios,
    expected: &libc::termios,
) {
    assert_eq!(actual.c_iflag, expected.c_iflag);
    assert_eq!(actual.c_oflag, expected.c_oflag);
    assert_eq!(actual.c_cflag, expected.c_cflag);
    assert_eq!(
        actual.c_lflag & (libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG | libc::NOFLSH),
        expected.c_lflag & (libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG | libc::NOFLSH)
    );
    assert_eq!(actual.c_cc, expected.c_cc);
}

#[cfg(unix)]
fn assert_raw_termios_matches_python_setraw(actual: &libc::termios, original: &libc::termios) {
    let expected = make_raw_termios(*original);
    assert_eq!(
        actual.c_iflag, expected.c_iflag,
        "raw mode should clear Python tty.setraw input flags"
    );
    assert_eq!(
        actual.c_oflag, expected.c_oflag,
        "raw mode should clear Python tty.setraw output flags"
    );
    assert_eq!(actual.c_cflag & libc::PARENB, 0);
    assert_eq!(actual.c_cflag & libc::CSIZE, libc::CS8);
    assert_eq!(actual.c_cflag & libc::HUPCL, original.c_cflag & libc::HUPCL);
    assert_eq!(
        actual.c_lflag & (libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG),
        0
    );
    assert_eq!(
        actual.c_lflag & libc::NOFLSH,
        original.c_lflag & libc::NOFLSH
    );
    assert_eq!(actual.c_cc[libc::VMIN], 1);
    assert_eq!(actual.c_cc[libc::VTIME], 0);
}

#[cfg(unix)]
#[test]
fn raw_terminal_mode_guard_sets_raw_mode_and_restores_on_drop() {
    let pty = PseudoTerminal::open();
    let original = current_termios(pty.slave);
    {
        let _guard = RawTerminalModeGuard::enter(pty.slave).unwrap();
        let raw = current_termios(pty.slave);
        assert_raw_termios_matches_python_setraw(&raw, &original);
    }

    let restored = current_termios(pty.slave);
    assert_restored_termios_matches_python_restore(&restored, &original);
}

#[cfg(unix)]
#[test]
fn raw_input_capture_enters_raw_mode_writes_sequences_and_restores_on_drop() {
    let pty = PseudoTerminal::open();
    let original = current_termios(pty.slave);
    let expected_enter = joined(terminal_mode_enter_sequences());
    let expected_exit = joined(terminal_mode_exit_sequences());
    let capture = RawInputCapture::enter(pty.slave).unwrap();

    let raw = current_termios(pty.slave);
    assert_raw_termios_matches_python_setraw(&raw, &original);
    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );

    let exit_handle = thread::spawn(move || drop(capture));
    assert_eq!(
        read_fd_exact(pty.master, expected_exit.len()),
        expected_exit
    );
    exit_handle.join().unwrap();

    let restored = current_termios(pty.slave);
    assert_restored_termios_matches_python_restore(&restored, &original);
}

#[cfg(unix)]
#[test]
fn raw_input_capture_read_key_delegates_to_terminal_reader() {
    let pty = PseudoTerminal::open();
    let capture = RawInputCapture::enter(pty.slave).unwrap();
    let expected_enter = joined(terminal_mode_enter_sequences());
    assert_eq!(
        read_fd_exact(pty.master, expected_enter.len()),
        expected_enter
    );
    write_fd(pty.master, b"a");

    let event = capture
        .read_key(Some(Duration::from_secs(1)))
        .unwrap()
        .expect("key event");

    assert_eq!(event.key, "a");
    assert_eq!(event.char_text, "a");

    let expected_exit = joined(terminal_mode_exit_sequences());
    let exit_handle = thread::spawn(move || drop(capture));
    assert_eq!(
        read_fd_exact(pty.master, expected_exit.len()),
        expected_exit
    );
    exit_handle.join().unwrap();
}
