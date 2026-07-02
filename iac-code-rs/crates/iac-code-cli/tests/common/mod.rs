use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub struct CommandFixture {
    pub argv: Vec<String>,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub fn command_fixture(group: &str, name: &str) -> CommandFixture {
    let path = workspace_root()
        .join("fixtures")
        .join("compatibility")
        .join(group)
        .join(format!("{name}.json"));
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("failed to read {}: {err}", path.display()));
    CommandFixture {
        argv: json_string_array_field(&text, "argv"),
        exit_code: json_i32_field(&text, "exit_code"),
        stdout: json_string_field(&text, "stdout"),
        stderr: json_string_field(&text, "stderr"),
    }
}

pub fn json_i32_field(text: &str, field: &str) -> i32 {
    let marker = format!("\"{field}\":");
    let start = text
        .find(&marker)
        .unwrap_or_else(|| panic!("missing field {field}"))
        + marker.len();
    let rest = text[start..].trim_start();
    let end = rest.find([',', '\n', '}']).unwrap_or(rest.len());
    rest[..end]
        .trim()
        .parse()
        .unwrap_or_else(|err| panic!("invalid integer field {field}: {err}"))
}

pub fn json_string_field(text: &str, field: &str) -> String {
    let marker = format!("\"{field}\":");
    let start = text
        .find(&marker)
        .unwrap_or_else(|| panic!("missing field {field}"))
        + marker.len();
    parse_json_string(text[start..].trim_start()).0
}

pub fn json_string_array_field(text: &str, field: &str) -> Vec<String> {
    let marker = format!("\"{field}\":");
    let mut rest = text[text
        .find(&marker)
        .unwrap_or_else(|| panic!("missing field {field}"))
        + marker.len()..]
        .trim_start();
    assert!(rest.starts_with('['), "field {field} is not an array");
    rest = rest[1..].trim_start();
    let mut values = Vec::new();
    while !rest.starts_with(']') {
        let (value, consumed) = parse_json_string(rest);
        values.push(value);
        rest = rest[consumed..].trim_start();
        if rest.starts_with(',') {
            rest = rest[1..].trim_start();
        }
    }
    values
}

pub fn read_http_request(stream: &mut impl Read) -> String {
    let mut buffer = [0_u8; 4096];
    let mut request = String::new();
    loop {
        let bytes_read = stream.read(&mut buffer).expect("read request");
        if bytes_read == 0 {
            break;
        }
        request.push_str(&String::from_utf8_lossy(&buffer[..bytes_read]));
        if request_is_complete(request.as_bytes()) {
            break;
        }
    }
    request
}

#[allow(dead_code)]
pub fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    workspace_target_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}

fn parse_json_string(text: &str) -> (String, usize) {
    assert!(text.starts_with('"'), "expected JSON string");
    let mut out = String::new();
    let mut escaped = false;
    for (idx, ch) in text[1..].char_indices() {
        if escaped {
            match ch {
                '"' => out.push('"'),
                '\\' => out.push('\\'),
                '/' => out.push('/'),
                'b' => out.push('\u{0008}'),
                'f' => out.push('\u{000c}'),
                'n' => out.push('\n'),
                'r' => out.push('\r'),
                't' => out.push('\t'),
                other => panic!("unsupported JSON escape: {other}"),
            }
            escaped = false;
        } else if ch == '\\' {
            escaped = true;
        } else if ch == '"' {
            return (out, idx + 2);
        } else {
            out.push(ch);
        }
    }
    panic!("unterminated JSON string")
}

fn request_is_complete(bytes: &[u8]) -> bool {
    let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return false;
    };
    let header_text = String::from_utf8_lossy(&bytes[..header_end]);
    let content_length = header_text
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);
    bytes.len() >= header_end + 4 + content_length
}

#[allow(dead_code)]
fn workspace_target_dir() -> PathBuf {
    workspace_root().join("target")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .expect("workspace root")
        .to_path_buf()
}
