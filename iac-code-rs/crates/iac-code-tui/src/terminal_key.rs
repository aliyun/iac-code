use crate::PromptKeyEvent;
#[cfg(unix)]
use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::time::Duration;

const BRACKETED_PASTE_START: &[u8] = b"[200~";
const BRACKETED_PASTE_END: &[u8] = b"\x1b[201~";

pub fn parse_terminal_key_byte(byte: u8) -> PromptKeyEvent {
    match byte {
        10 | 13 => PromptKeyEvent::new("enter", char::from(byte).to_string()),
        9 => PromptKeyEvent::new("tab", "\t"),
        127 => PromptKeyEvent::new("backspace", char::from(127).to_string()),
        27 => PromptKeyEvent::new("escape", "\x1b"),
        1..=26 => {
            let letter = char::from(b'a' + byte - 1).to_string();
            PromptKeyEvent::new(letter.clone(), char::from(byte).to_string()).with_ctrl(true)
        }
        32..=126 => {
            let ch = char::from(byte);
            PromptKeyEvent::new(ch.to_string(), ch.to_string()).with_shift(ch.is_ascii_uppercase())
        }
        _ => PromptKeyEvent::new("unknown", char::from(byte).to_string()),
    }
}

pub fn decode_terminal_input(bytes: &[u8]) -> Option<PromptKeyEvent> {
    let (&first, rest) = bytes.split_first()?;

    if first == 27 {
        if rest.is_empty() {
            return Some(parse_terminal_key_byte(first));
        }
        if is_bracketed_paste_start(rest) {
            return Some(parse_bracketed_paste_bytes(
                &rest[BRACKETED_PASTE_START.len()..],
            ));
        }
        let sequence = String::from_utf8_lossy(rest);
        return Some(parse_terminal_escape_sequence(&sequence));
    }

    if first >= 0x80 {
        return Some(parse_utf8_key(bytes));
    }

    Some(parse_terminal_key_byte(first))
}

#[cfg(unix)]
pub fn read_terminal_key(
    fd: RawFd,
    timeout: Option<Duration>,
) -> io::Result<Option<PromptKeyEvent>> {
    if let Some(timeout) = timeout {
        if !wait_fd_readable(fd, timeout)? {
            return Ok(None);
        }
    }

    let first = read_fd_bytes(fd, 1)?;
    let Some(&byte) = first.first() else {
        return Ok(None);
    };

    if byte == 27 {
        if !wait_fd_readable(fd, Duration::from_millis(50))? {
            return Ok(Some(parse_terminal_key_byte(byte)));
        }

        let rest = read_fd_bytes(fd, 64)?;
        if is_bracketed_paste_start(&rest) {
            let mut paste = rest[BRACKETED_PASTE_START.len()..].to_vec();
            read_bracketed_paste_from_fd(fd, &mut paste)?;
            return Ok(Some(parse_bracketed_paste_bytes(&paste)));
        }

        let mut bytes = Vec::with_capacity(1 + rest.len());
        bytes.push(byte);
        bytes.extend(rest);
        return Ok(decode_terminal_input(&bytes));
    }

    if byte >= 0x80 {
        let mut bytes = first;
        for _ in 0..utf8_continuation_bytes(byte) {
            let extra = read_fd_bytes(fd, 1)?;
            if extra.is_empty() {
                break;
            }
            bytes.extend(extra);
        }
        return Ok(decode_terminal_input(&bytes));
    }

    Ok(Some(parse_terminal_key_byte(byte)))
}

pub fn is_bracketed_paste_start(bytes_after_escape: &[u8]) -> bool {
    bytes_after_escape.starts_with(BRACKETED_PASTE_START)
}

pub fn parse_bracketed_paste_bytes(bytes_after_start_marker: &[u8]) -> PromptKeyEvent {
    let content = match find_bytes(bytes_after_start_marker, BRACKETED_PASTE_END) {
        Some(index) => &bytes_after_start_marker[..index],
        None => bytes_after_start_marker,
    };
    let text = String::from_utf8_lossy(content)
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    PromptKeyEvent::new("paste", text)
}

pub fn parse_terminal_escape_sequence(sequence: &str) -> PromptKeyEvent {
    if let Some(key) = named_escape_sequence(sequence) {
        return PromptKeyEvent::new(key, "");
    }

    if let Some(event) = parse_modified_key_sequence(sequence) {
        return event;
    }

    if let Some(event) = parse_mouse_sgr_sequence(sequence) {
        return event;
    }

    let mut chars = sequence.chars();
    if let (Some(ch), None) = (chars.next(), chars.next()) {
        if ch.is_ascii_graphic() || ch == ' ' {
            return PromptKeyEvent::new(ch.to_string(), ch.to_string()).with_alt(true);
        }
    }

    PromptKeyEvent::new("unknown", "")
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

#[cfg(unix)]
fn read_bracketed_paste_from_fd(fd: RawFd, bytes: &mut Vec<u8>) -> io::Result<()> {
    while find_bytes(bytes, BRACKETED_PASTE_END).is_none() {
        if !wait_fd_readable(fd, Duration::from_secs(1))? {
            break;
        }
        let chunk = read_fd_bytes(fd, 4096)?;
        if chunk.is_empty() {
            break;
        }
        bytes.extend(chunk);
    }
    Ok(())
}

#[cfg(unix)]
fn utf8_continuation_bytes(first: u8) -> usize {
    if first < 0xc0 {
        0
    } else if first < 0xe0 {
        1
    } else if first < 0xf0 {
        2
    } else {
        3
    }
}

#[cfg(unix)]
fn read_fd_bytes(fd: RawFd, max_len: usize) -> io::Result<Vec<u8>> {
    let mut bytes = vec![0; max_len];
    let status = unsafe { libc::read(fd, bytes.as_mut_ptr().cast(), max_len) };
    if status < 0 {
        return Err(io::Error::last_os_error());
    }
    bytes.truncate(status as usize);
    Ok(bytes)
}

#[cfg(unix)]
fn wait_fd_readable(fd: RawFd, timeout: Duration) -> io::Result<bool> {
    let mut readfds: libc::fd_set = unsafe { std::mem::zeroed() };
    unsafe {
        libc::FD_ZERO(&mut readfds);
        libc::FD_SET(fd, &mut readfds);
    }
    let mut timeout = duration_to_timeval(timeout);
    let status = unsafe {
        libc::select(
            fd + 1,
            &mut readfds,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut timeout,
        )
    };
    if status < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(status > 0)
    }
}

#[cfg(unix)]
fn duration_to_timeval(timeout: Duration) -> libc::timeval {
    libc::timeval {
        tv_sec: timeout.as_secs() as libc::time_t,
        tv_usec: timeout.subsec_micros() as libc::suseconds_t,
    }
}

fn parse_utf8_key(bytes: &[u8]) -> PromptKeyEvent {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return PromptKeyEvent::new("unknown", "");
    };
    let Some(ch) = text.chars().next() else {
        return PromptKeyEvent::new("unknown", "");
    };
    let text = ch.to_string();
    PromptKeyEvent::new(text.clone(), text)
}

fn named_escape_sequence(sequence: &str) -> Option<&'static str> {
    match sequence {
        "[A" => Some("up"),
        "[B" => Some("down"),
        "[C" => Some("right"),
        "[D" => Some("left"),
        "[H" => Some("home"),
        "[F" => Some("end"),
        "[3~" => Some("delete"),
        "[5~" => Some("pageup"),
        "[6~" => Some("pagedown"),
        "[I" => Some("focus_in"),
        "[O" => Some("focus_out"),
        "OP" => Some("f1"),
        "OQ" => Some("f2"),
        "OR" => Some("f3"),
        "OS" => Some("f4"),
        _ => None,
    }
}

fn parse_modified_key_sequence(sequence: &str) -> Option<PromptKeyEvent> {
    if let Some((codepoint, modifier)) = parse_csi_u(sequence) {
        return event_from_codepoint(codepoint, modifier);
    }

    if let Some((codepoint, modifier)) = parse_xterm_modify_other_keys(sequence) {
        return event_from_codepoint(codepoint, modifier);
    }

    if let Some((codepoint, modifier)) = parse_modified_special_key(sequence) {
        return event_from_codepoint(codepoint, modifier);
    }

    None
}

fn parse_csi_u(sequence: &str) -> Option<(u32, u32)> {
    let body = sequence.strip_prefix('[')?.strip_suffix('u')?;
    let (codepoint, modifier) = body.split_once(';')?;
    Some((codepoint.parse().ok()?, modifier.parse().ok()?))
}

fn parse_xterm_modify_other_keys(sequence: &str) -> Option<(u32, u32)> {
    let body = sequence.strip_prefix("[27;")?.strip_suffix('~')?;
    let (modifier, codepoint) = body.split_once(';')?;
    Some((codepoint.parse().ok()?, modifier.parse().ok()?))
}

fn parse_modified_special_key(sequence: &str) -> Option<(u32, u32)> {
    let body = sequence.strip_prefix('[')?.strip_suffix('~')?;
    let (codepoint, modifier) = body.split_once(';')?;
    Some((codepoint.parse().ok()?, modifier.parse().ok()?))
}

fn event_from_codepoint(codepoint: u32, modifier: u32) -> Option<PromptKeyEvent> {
    let flags = modifier.saturating_sub(1);
    let shift = flags & 1 != 0;
    let alt = flags & 2 != 0;
    let ctrl = flags & 4 != 0;

    if matches!(codepoint, 10 | 13) {
        if shift && !alt && !ctrl {
            return Some(PromptKeyEvent::new("enter", "").with_shift(true));
        }
        return None;
    }

    if !(32..=0x10ffff).contains(&codepoint) {
        return None;
    }

    let ch = char::from_u32(codepoint)?;
    let key = if ctrl {
        ch.to_lowercase().collect::<String>()
    } else {
        ch.to_string()
    };
    Some(
        PromptKeyEvent::new(key, "")
            .with_ctrl(ctrl)
            .with_alt(alt)
            .with_shift(shift),
    )
}

fn parse_mouse_sgr_sequence(sequence: &str) -> Option<PromptKeyEvent> {
    let rest = sequence.strip_prefix("[<")?;
    let (button, rest) = rest.split_once(';')?;
    rest.split_once(';')?;
    let button: u32 = button.parse().ok()?;
    let key = match button {
        64 => "wheel_up",
        65 => "wheel_down",
        _ => "mouse",
    };
    Some(PromptKeyEvent::new(key, ""))
}
