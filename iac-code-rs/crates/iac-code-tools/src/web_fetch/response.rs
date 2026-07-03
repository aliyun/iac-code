use std::io::Read;

use super::html::extract_text_from_html;

const TRUNCATION_MARKER: &str = "\n\n[truncated]";

pub(super) fn read_limited(
    reader: &mut impl Read,
    content_length: Option<u64>,
    max_download_bytes: usize,
) -> Result<(Vec<u8>, bool), std::io::Error> {
    let mut downloaded = Vec::new();
    let mut buffer = [0_u8; 8192];

    loop {
        let remaining_bytes = max_download_bytes.saturating_sub(downloaded.len());
        if remaining_bytes == 0 {
            if content_length.is_some_and(|length| length > max_download_bytes as u64) {
                return Ok((downloaded, true));
            }
            let mut probe = [0_u8; 1];
            let extra_bytes = reader.read(&mut probe)?;
            return Ok((downloaded, extra_bytes > 0));
        }

        let bytes_to_read = remaining_bytes.min(buffer.len());
        let bytes_read = reader.read(&mut buffer[..bytes_to_read])?;
        if bytes_read == 0 {
            return Ok((downloaded, false));
        }
        downloaded.extend_from_slice(&buffer[..bytes_read]);
    }
}

pub(super) fn finish_content(
    mut text: String,
    content_type: &str,
    max_length: i64,
    download_truncated: bool,
) -> String {
    if content_type.contains("text/html") {
        text = extract_text_from_html(&text);
    }

    if max_length <= 0 {
        return String::new();
    }

    let max_length = max_length as usize;
    if download_truncated && max_length >= TRUNCATION_MARKER.chars().count() {
        let available_length = max_length - TRUNCATION_MARKER.chars().count();
        let mut output = take_chars(&text, available_length);
        output.push_str(TRUNCATION_MARKER);
        output
    } else if text.chars().count() > max_length {
        take_chars(&text, max_length)
    } else {
        text
    }
}

pub(super) fn decode_response_body(bytes: &[u8], content_type: &str) -> String {
    if let Some(charset) = content_type_charset(content_type) {
        if charset.eq_ignore_ascii_case("utf-8") || charset.eq_ignore_ascii_case("utf8") {
            return String::from_utf8_lossy(bytes).into_owned();
        }
        if charset.eq_ignore_ascii_case("iso-8859-1")
            || charset.eq_ignore_ascii_case("latin-1")
            || charset.eq_ignore_ascii_case("latin1")
        {
            return bytes.iter().map(|byte| char::from(*byte)).collect();
        }
    }

    String::from_utf8_lossy(bytes).into_owned()
}

fn content_type_charset(content_type: &str) -> Option<&str> {
    for parameter in content_type.split(';').skip(1) {
        let parameter = parameter.trim();
        let Some((name, value)) = parameter.split_once('=') else {
            continue;
        };
        if name.trim().eq_ignore_ascii_case("charset") {
            let value = value.trim().trim_matches('"').trim_matches('\'').trim();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

fn take_chars(input: &str, max_length: usize) -> String {
    input.chars().take(max_length).collect()
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;

    #[test]
    fn read_limited_marks_oversized_chunk_as_truncated() {
        let mut reader = Cursor::new(b"abcdefghi".to_vec());

        let (downloaded, truncated) = read_limited(&mut reader, None, 5).expect("read response");

        assert_eq!(downloaded, b"abcde");
        assert!(truncated);
    }

    #[test]
    fn read_limited_uses_content_length_to_detect_exact_cap_truncation() {
        let mut reader = Cursor::new(b"abcde".to_vec());

        let (downloaded, truncated) = read_limited(&mut reader, Some(6), 5).expect("read response");

        assert_eq!(downloaded, b"abcde");
        assert!(truncated);
    }

    #[test]
    fn finish_content_appends_truncation_marker_within_max_length() {
        let output = finish_content("abcdefghij".into(), "text/plain", 15, true);

        assert_eq!(output, "ab\n\n[truncated]");
    }
}
