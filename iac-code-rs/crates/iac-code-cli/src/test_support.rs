#[cfg(unix)]
use std::io;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
#[cfg(unix)]
use std::os::fd::RawFd;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iac_code_config::paths::ConfigPaths;
#[cfg(unix)]
use iac_code_tui::{terminal_display_width, SuggestionItem};
#[cfg(unix)]
use unicode_segmentation::UnicodeSegmentation;

#[cfg(unix)]
use crate::raw_prompt_input::{RawPromptImageSource, RawPromptPastedImage};
#[cfg(unix)]
use crate::raw_prompt_text::{
    raw_prompt_push_rendered_text_with_image_links, RAW_PROMPT_PREFIX_STYLED,
};

static TEST_ENV_LOCK: Mutex<()> = Mutex::new(());

pub(crate) struct EnvVarGuard {
    _lock: MutexGuard<'static, ()>,
    previous: Vec<(&'static str, Option<String>)>,
}

impl EnvVarGuard {
    pub(crate) fn set(key: &'static str, value: &str) -> Self {
        Self::set_many(&[(key, value)])
    }

    pub(crate) fn set_many(values: &[(&'static str, &str)]) -> Self {
        let lock = TEST_ENV_LOCK.lock().expect("test env lock");
        let previous = values
            .iter()
            .map(|(key, _)| (*key, std::env::var(key).ok()))
            .collect::<Vec<_>>();
        for (key, value) in values {
            std::env::set_var(key, value);
        }
        Self {
            _lock: lock,
            previous,
        }
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        for (key, previous) in &self.previous {
            if let Some(previous) = previous {
                std::env::set_var(key, previous);
            } else {
                std::env::remove_var(key);
            }
        }
    }
}

pub(crate) fn english_locale_guard() -> EnvVarGuard {
    EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
    ])
}

pub(crate) fn english_locale_config_dir_guard(config_dir: &Path) -> EnvVarGuard {
    let config_dir_text = config_dir
        .to_str()
        .expect("config dir should be valid unicode")
        .to_owned();
    EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("IAC_CODE_CONFIG_DIR", &config_dir_text),
    ])
}

pub(crate) fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    workspace_target_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}

fn workspace_target_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .ancestors()
        .nth(2)
        .expect("workspace root")
        .join("target")
}

#[cfg(unix)]
pub(crate) struct StaticRawPromptImageSource {
    images: Vec<RawPromptPastedImage>,
    has_image_results: Vec<bool>,
}

#[cfg(unix)]
impl StaticRawPromptImageSource {
    pub(crate) fn new(images: Vec<RawPromptPastedImage>) -> Self {
        Self {
            images,
            has_image_results: Vec::new(),
        }
    }

    pub(crate) fn new_with_has_image_results(
        images: Vec<RawPromptPastedImage>,
        has_image_results: Vec<bool>,
    ) -> Self {
        Self {
            images,
            has_image_results,
        }
    }
}

#[cfg(unix)]
impl RawPromptImageSource for StaticRawPromptImageSource {
    fn has_image(&mut self) -> std::io::Result<bool> {
        if self.has_image_results.is_empty() {
            Ok(!self.images.is_empty())
        } else {
            Ok(self.has_image_results.remove(0))
        }
    }

    fn read_image(&mut self) -> std::io::Result<Option<RawPromptPastedImage>> {
        if self.images.is_empty() {
            Ok(None)
        } else {
            Ok(Some(self.images.remove(0)))
        }
    }
}

#[cfg(unix)]
pub(crate) struct PseudoTerminal {
    pub(crate) master: RawFd,
    pub(crate) slave: RawFd,
}

#[cfg(unix)]
impl PseudoTerminal {
    pub(crate) fn open() -> Self {
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

    pub(crate) fn set_size(&self, rows: u16, columns: u16) {
        let mut size = libc::winsize {
            ws_row: rows,
            ws_col: columns,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        let status = unsafe { libc::ioctl(self.slave, libc::TIOCSWINSZ, &mut size) };
        assert_eq!(
            status,
            0,
            "TIOCSWINSZ failed: {}",
            std::io::Error::last_os_error()
        );
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
pub(crate) fn terminal_mode_bytes(sequences: &[&[u8]]) -> Vec<u8> {
    sequences.concat()
}

#[cfg(unix)]
pub(crate) fn raw_prompt_render(text: &str) -> Vec<u8> {
    let image_links = std::collections::BTreeMap::new();
    let mut output = format!("\r\x1b[2K{RAW_PROMPT_PREFIX_STYLED}");
    raw_prompt_push_rendered_text_with_image_links(&mut output, text, &image_links);
    output.into_bytes()
}

#[cfg(unix)]
pub(crate) fn raw_prompt_text_fragment(text: &str) -> Vec<u8> {
    let image_links = std::collections::BTreeMap::new();
    let mut output = RAW_PROMPT_PREFIX_STYLED.to_owned();
    raw_prompt_push_rendered_text_with_image_links(&mut output, text, &image_links);
    output.into_bytes()
}

#[cfg(unix)]
pub(crate) fn raw_prompt_render_with_ghost(text: &str, ghost: &str) -> Vec<u8> {
    format!("{RAW_PROMPT_PREFIX_STYLED}{text}\x1b[2m{ghost}\x1b[0m").into_bytes()
}

#[cfg(unix)]
pub(crate) fn raw_prompt_test_suggestion(
    display_text: &str,
    completion: &str,
    description: &str,
) -> SuggestionItem {
    SuggestionItem {
        id: display_text.to_owned(),
        display_text: display_text.to_owned(),
        completion: completion.to_owned(),
        description: Some(description.to_owned()),
        icon: None,
        source: "test".to_owned(),
        score: 1.0,
        arg_hint: None,
    }
}

#[cfg(unix)]
pub(crate) fn empty_skill_catalog() -> iac_code_tui::SkillCatalog {
    iac_code_tui::SkillCatalog::new()
}

#[cfg(unix)]
pub(crate) fn raw_visible_lines_from_terminal_output(output: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(output);
    let normalized = text.replace("\r\n", "\n").replace('\r', "");
    normalized
        .split('\n')
        .map(|line| raw_strip_ansi_sequences(&line.replace("\x1b[2K", "")))
        .filter(|line| !line.is_empty())
        .collect()
}

#[cfg(unix)]
pub(crate) fn raw_strip_ansi_sequences(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for sequence_ch in chars.by_ref() {
                if ('@'..='~').contains(&sequence_ch) {
                    break;
                }
            }
            continue;
        }
        output.push(ch);
    }
    output
}

#[cfg(unix)]
#[derive(Debug)]
pub(crate) struct RawAnsiScreenSnapshot {
    pub(crate) lines: Vec<String>,
    pub(crate) cursor: (usize, usize),
}

#[cfg(unix)]
pub(crate) fn raw_ansi_screen_after_writes(
    columns: u16,
    rows: u16,
    writes: &[&[u8]],
) -> RawAnsiScreenSnapshot {
    let mut screen = RawAnsiScreen::new(columns as usize, rows as usize);
    for write in writes {
        screen.write(write);
    }
    screen.snapshot()
}

#[cfg(unix)]
struct RawAnsiScreen {
    columns: usize,
    rows: usize,
    cursor_row: usize,
    cursor_col: usize,
    saved_cursor: Option<(usize, usize)>,
    cells: Vec<Vec<String>>,
}

#[cfg(unix)]
impl RawAnsiScreen {
    fn new(columns: usize, rows: usize) -> Self {
        let columns = columns.max(1);
        let rows = rows.max(1);
        Self {
            columns,
            rows,
            cursor_row: 0,
            cursor_col: 0,
            saved_cursor: None,
            cells: vec![vec![String::new(); columns]; rows],
        }
    }

    fn write(&mut self, bytes: &[u8]) {
        let text = String::from_utf8_lossy(bytes);
        let mut index = 0;
        while index < text.len() {
            let next = text[index..].chars().next().expect("valid char");
            if next == '\x1b' {
                index += next.len_utf8();
                if text[index..].starts_with('[') {
                    index += 1;
                    let start = index;
                    while index < text.len() {
                        let ch = text[index..].chars().next().expect("valid CSI char");
                        index += ch.len_utf8();
                        if ('@'..='~').contains(&ch) {
                            self.apply_csi(&text[start..index - ch.len_utf8()], ch);
                            break;
                        }
                    }
                }
                continue;
            }
            if next == '\r' {
                self.cursor_col = 0;
                index += next.len_utf8();
                continue;
            }
            if next == '\n' {
                self.line_feed();
                index += next.len_utf8();
                continue;
            }

            let grapheme = text[index..]
                .graphemes(true)
                .next()
                .expect("valid grapheme");
            self.write_grapheme(grapheme);
            index += grapheme.len();
        }
    }

    fn apply_csi(&mut self, parameters: &str, command: char) {
        match command {
            'A' => {
                let amount = raw_ansi_first_parameter(parameters).unwrap_or(1);
                self.cursor_row = self.cursor_row.saturating_sub(amount);
            }
            'B' => {
                let amount = raw_ansi_first_parameter(parameters).unwrap_or(1);
                self.cursor_row = (self.cursor_row + amount).min(self.rows - 1);
            }
            'C' => {
                let amount = raw_ansi_first_parameter(parameters).unwrap_or(1);
                self.cursor_col = (self.cursor_col + amount).min(self.columns - 1);
            }
            'D' => {
                let amount = raw_ansi_first_parameter(parameters).unwrap_or(1);
                self.cursor_col = self.cursor_col.saturating_sub(amount);
            }
            'H' | 'f' => {
                let mut parts = parameters.split(';');
                let row = parts
                    .next()
                    .and_then(raw_ansi_parse_usize)
                    .unwrap_or(1)
                    .saturating_sub(1);
                let col = parts
                    .next()
                    .and_then(raw_ansi_parse_usize)
                    .unwrap_or(1)
                    .saturating_sub(1);
                self.cursor_row = row.min(self.rows - 1);
                self.cursor_col = col.min(self.columns - 1);
            }
            'J' if parameters == "2" => self.clear_screen(),
            'K' if parameters.is_empty() || parameters == "0" => {
                self.clear_line_from_cursor();
            }
            'K' if parameters == "2" => self.clear_line(),
            'm' => {}
            's' => {
                self.saved_cursor = Some((self.cursor_row, self.cursor_col));
            }
            'u' => {
                if let Some((row, col)) = self.saved_cursor {
                    self.cursor_row = row.min(self.rows - 1);
                    self.cursor_col = col.min(self.columns - 1);
                }
            }
            'h' | 'l' if parameters.starts_with('?') => {}
            _ => {}
        }
    }

    fn write_grapheme(&mut self, grapheme: &str) {
        let width = terminal_display_width(grapheme);
        if width == 0 {
            return;
        }
        if self.cursor_col > 0 && self.cursor_col.saturating_add(width) > self.columns {
            self.line_feed();
            self.cursor_col = 0;
        }
        if self.cursor_row >= self.rows {
            self.scroll_up();
        }
        self.cells[self.cursor_row][self.cursor_col] = grapheme.to_owned();
        for offset in 1..width {
            if self.cursor_col + offset < self.columns {
                self.cells[self.cursor_row][self.cursor_col + offset].clear();
            }
        }
        self.cursor_col = (self.cursor_col + width).min(self.columns);
    }

    fn line_feed(&mut self) {
        if self.cursor_row + 1 >= self.rows {
            self.scroll_up();
        } else {
            self.cursor_row += 1;
        }
    }

    fn scroll_up(&mut self) {
        self.cells.remove(0);
        self.cells.push(vec![String::new(); self.columns]);
        self.cursor_row = self.rows - 1;
    }

    fn clear_screen(&mut self) {
        for row in &mut self.cells {
            for cell in row {
                cell.clear();
            }
        }
    }

    fn clear_line(&mut self) {
        for cell in &mut self.cells[self.cursor_row] {
            cell.clear();
        }
    }

    fn clear_line_from_cursor(&mut self) {
        for col in self.cursor_col..self.columns {
            self.cells[self.cursor_row][col].clear();
        }
    }

    fn snapshot(&self) -> RawAnsiScreenSnapshot {
        RawAnsiScreenSnapshot {
            lines: self
                .cells
                .iter()
                .map(|row| row.concat().trim_end().to_owned())
                .collect(),
            cursor: (self.cursor_row, self.cursor_col),
        }
    }
}

#[cfg(unix)]
fn raw_ansi_first_parameter(parameters: &str) -> Option<usize> {
    raw_ansi_parse_usize(parameters.split(';').next().unwrap_or_default())
}

#[cfg(unix)]
fn raw_ansi_parse_usize(parameter: &str) -> Option<usize> {
    parameter
        .trim_start_matches('?')
        .parse::<usize>()
        .ok()
        .filter(|value| *value > 0)
}

#[cfg(unix)]
pub(crate) fn assert_bytes_contains(output: &[u8], needle: &[u8]) {
    assert!(
        output.windows(needle.len()).any(|window| window == needle),
        "expected output to contain {:?}; got {:?}",
        String::from_utf8_lossy(needle),
        String::from_utf8_lossy(output)
    );
}

#[cfg(unix)]
pub(crate) fn write_fd(fd: RawFd, bytes: &[u8]) {
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
pub(crate) fn read_fd_exact(fd: RawFd, len: usize) -> Vec<u8> {
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
pub(crate) fn read_fd_until_contains(fd: RawFd, needle: &[u8]) -> Vec<u8> {
    let mut bytes = Vec::new();
    while !bytes.windows(needle.len()).any(|window| window == needle) {
        assert!(
            wait_fd_readable(fd, Duration::from_secs(1)),
            "timed out waiting for {:?}; got {:?}",
            String::from_utf8_lossy(needle),
            String::from_utf8_lossy(&bytes)
        );
        let mut chunk = [0_u8; 4096];
        let status = unsafe { libc::read(fd, chunk.as_mut_ptr().cast(), chunk.len()) };
        assert!(
            status > 0,
            "read failed: {}",
            std::io::Error::last_os_error()
        );
        bytes.extend_from_slice(&chunk[..status as usize]);
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

pub(crate) fn paths_for(config_dir: &std::path::Path) -> ConfigPaths {
    ConfigPaths {
        config_dir: config_dir.to_path_buf(),
        credentials_path: config_dir.join(".credentials.yml"),
        settings_path: config_dir.join("settings.yml"),
        cloud_credentials_path: config_dir.join(".cloud-credentials.yml"),
        history_path: config_dir.join(".input_history"),
    }
}

pub(crate) fn read_test_http_request(stream: &mut TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("stream should have read timeout");
    let mut buffer = [0_u8; 4096];
    let mut request = String::new();
    loop {
        let bytes_read = stream.read(&mut buffer).expect("read request");
        if bytes_read == 0 {
            break;
        }
        request.push_str(&String::from_utf8_lossy(&buffer[..bytes_read]));
        if request.contains("\r\n\r\n") {
            break;
        }
    }
    let Some(header_end) = request.find("\r\n\r\n") else {
        return request;
    };
    let headers = request[..header_end].to_ascii_lowercase();
    let content_length = headers
        .lines()
        .find_map(|line| line.strip_prefix("content-length:"))
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(0);
    let mut body_bytes = request[header_end + 4..].len();
    while body_bytes < content_length {
        let bytes_read = stream.read(&mut buffer).expect("read request body");
        if bytes_read == 0 {
            break;
        }
        body_bytes += bytes_read;
        request.push_str(&String::from_utf8_lossy(&buffer[..bytes_read]));
    }
    request
}

pub(crate) fn accept_test_with_timeout(listener: TcpListener) -> (TcpStream, std::net::SocketAddr) {
    let deadline = SystemTime::now() + Duration::from_secs(5);
    loop {
        match listener.accept() {
            Ok((stream, addr)) => {
                stream
                    .set_nonblocking(false)
                    .expect("test stream should be blocking");
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .expect("test stream should have read timeout");
                stream
                    .set_write_timeout(Some(Duration::from_secs(5)))
                    .expect("test stream should have write timeout");
                return (stream, addr);
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                if SystemTime::now() >= deadline {
                    panic!("timed out waiting for test connection");
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => panic!("accept failed: {error}"),
        }
    }
}

pub(crate) fn write_test_http_response(stream: &mut TcpStream, body: &str) {
    write!(
        stream,
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
        body.len(),
        body
    )
    .expect("write response");
}
