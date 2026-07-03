use std::io::{self, Write};
#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::time::Duration;

#[cfg(unix)]
use crate::{terminal_key::read_terminal_key, PromptKeyEvent};

pub const TERMINAL_MODE_ENTER_SEQUENCES: [&[u8]; 4] =
    [b"\x1b[?2004h", b"\x1b[?1004h", b"\x1b[>1u", b"\x1b[>4;2m"];

pub const TERMINAL_MODE_EXIT_SEQUENCES: [&[u8]; 4] =
    [b"\x1b[>4;0m", b"\x1b[<u", b"\x1b[?1004l", b"\x1b[?2004l"];

#[derive(Debug)]
pub struct TerminalModeGuard<W: Write> {
    writer: W,
    active: bool,
}

impl<W: Write> TerminalModeGuard<W> {
    pub fn enter(mut writer: W) -> io::Result<Self> {
        write_terminal_mode_enter_sequences(&mut writer)?;
        Ok(Self {
            writer,
            active: true,
        })
    }

    pub fn exit(&mut self) -> io::Result<()> {
        if self.active {
            write_terminal_mode_exit_sequences(&mut self.writer)?;
            self.active = false;
        }
        Ok(())
    }

    pub fn writer_mut(&mut self) -> &mut W {
        &mut self.writer
    }
}

impl<W: Write> Drop for TerminalModeGuard<W> {
    fn drop(&mut self) {
        let _ = self.exit();
    }
}

#[cfg(unix)]
#[derive(Debug)]
pub struct RawTerminalModeGuard {
    fd: RawFd,
    original: libc::termios,
    active: bool,
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TerminalDimensions {
    pub rows: usize,
    pub columns: usize,
}

#[cfg(unix)]
impl RawTerminalModeGuard {
    pub fn enter(fd: RawFd) -> io::Result<Self> {
        let original = get_termios(fd)?;
        let raw = make_raw_termios(original);
        set_termios(fd, libc::TCSAFLUSH, &raw)?;
        Ok(Self {
            fd,
            original,
            active: true,
        })
    }

    pub fn exit(&mut self) -> io::Result<()> {
        if self.active {
            set_termios(self.fd, libc::TCSADRAIN, &self.original)?;
            self.active = false;
        }
        Ok(())
    }

    pub fn fd(&self) -> RawFd {
        self.fd
    }
}

#[cfg(unix)]
pub fn terminal_dimensions(fd: RawFd) -> io::Result<Option<TerminalDimensions>> {
    let mut size: libc::winsize = unsafe { std::mem::zeroed() };
    let status = unsafe { libc::ioctl(fd, libc::TIOCGWINSZ, &mut size) };
    if status != 0 {
        return Err(io::Error::last_os_error());
    }

    if size.ws_row == 0 || size.ws_col == 0 {
        return Ok(None);
    }

    Ok(Some(TerminalDimensions {
        rows: usize::from(size.ws_row),
        columns: usize::from(size.ws_col),
    }))
}

#[cfg(unix)]
impl Drop for RawTerminalModeGuard {
    fn drop(&mut self) {
        let _ = self.exit();
    }
}

#[cfg(unix)]
#[derive(Debug)]
pub struct RawInputCapture {
    fd: RawFd,
    raw_mode: RawTerminalModeGuard,
    sequences_active: bool,
}

#[cfg(unix)]
impl RawInputCapture {
    pub fn enter(fd: RawFd) -> io::Result<Self> {
        let raw_mode = RawTerminalModeGuard::enter(fd)?;
        write_terminal_mode_enter_sequences_to_fd(fd)?;
        Ok(Self {
            fd,
            raw_mode,
            sequences_active: true,
        })
    }

    pub fn exit(&mut self) -> io::Result<()> {
        let sequence_result = if self.sequences_active {
            let result = write_terminal_mode_exit_sequences_to_fd(self.fd);
            if result.is_ok() {
                self.sequences_active = false;
            }
            result
        } else {
            Ok(())
        };
        let raw_result = self.raw_mode.exit();
        sequence_result?;
        raw_result
    }

    pub fn read_key(&self, timeout: Option<Duration>) -> io::Result<Option<PromptKeyEvent>> {
        read_terminal_key(self.fd, timeout)
    }

    pub fn fd(&self) -> RawFd {
        self.fd
    }
}

#[cfg(unix)]
impl Drop for RawInputCapture {
    fn drop(&mut self) {
        let _ = self.exit();
    }
}

#[cfg(unix)]
pub fn make_raw_termios(mut termios: libc::termios) -> libc::termios {
    termios.c_iflag &= !(libc::BRKINT | libc::ICRNL | libc::INPCK | libc::ISTRIP | libc::IXON);
    termios.c_oflag &= !libc::OPOST;
    termios.c_cflag &= !(libc::CSIZE | libc::PARENB);
    termios.c_cflag |= libc::CS8;
    termios.c_lflag &= !(libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG);
    termios.c_cc[libc::VMIN] = 1;
    termios.c_cc[libc::VTIME] = 0;
    termios
}

#[cfg(unix)]
fn get_termios(fd: RawFd) -> io::Result<libc::termios> {
    let mut termios: libc::termios = unsafe { std::mem::zeroed() };
    let status = unsafe { libc::tcgetattr(fd, &mut termios) };
    if status == 0 {
        Ok(termios)
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn set_termios(
    fd: RawFd,
    optional_actions: libc::c_int,
    termios: &libc::termios,
) -> io::Result<()> {
    let status = unsafe { libc::tcsetattr(fd, optional_actions, termios) };
    if status == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn write_terminal_mode_enter_sequences_to_fd(fd: RawFd) -> io::Result<()> {
    write_terminal_mode_sequences_to_fd(fd, terminal_mode_enter_sequences())
}

#[cfg(unix)]
fn write_terminal_mode_exit_sequences_to_fd(fd: RawFd) -> io::Result<()> {
    write_terminal_mode_sequences_to_fd(fd, terminal_mode_exit_sequences())
}

#[cfg(unix)]
fn write_terminal_mode_sequences_to_fd(fd: RawFd, sequences: &[&[u8]]) -> io::Result<()> {
    for sequence in sequences {
        write_fd_all(fd, sequence)?;
    }
    Ok(())
}

#[cfg(unix)]
fn write_fd_all(fd: RawFd, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let status = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if status < 0 {
            return Err(io::Error::last_os_error());
        }
        if status == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "terminal fd write returned zero bytes",
            ));
        }
        bytes = &bytes[status as usize..];
    }
    Ok(())
}

pub fn terminal_mode_enter_sequences() -> &'static [&'static [u8]] {
    &TERMINAL_MODE_ENTER_SEQUENCES
}

pub fn terminal_mode_exit_sequences() -> &'static [&'static [u8]] {
    &TERMINAL_MODE_EXIT_SEQUENCES
}

pub fn write_terminal_mode_enter_sequences(writer: &mut impl Write) -> io::Result<()> {
    write_terminal_mode_sequences(writer, terminal_mode_enter_sequences())
}

pub fn write_terminal_mode_exit_sequences(writer: &mut impl Write) -> io::Result<()> {
    write_terminal_mode_sequences(writer, terminal_mode_exit_sequences())
}

fn write_terminal_mode_sequences(writer: &mut impl Write, sequences: &[&[u8]]) -> io::Result<()> {
    for sequence in sequences {
        writer.write_all(sequence)?;
    }
    writer.flush()
}
