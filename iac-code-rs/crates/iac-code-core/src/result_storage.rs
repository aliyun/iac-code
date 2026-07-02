use std::fs;
use std::path::{Path, PathBuf};

const DEFAULT_MAX_INLINE_CHARS: usize = 50_000;
const DEFAULT_PREVIEW_CHARS: usize = 2_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResultStorage {
    storage_dir: PathBuf,
    max_inline_chars: usize,
    preview_chars: usize,
}

impl ResultStorage {
    pub fn new(storage_dir: impl Into<PathBuf>) -> Self {
        Self {
            storage_dir: storage_dir.into(),
            max_inline_chars: DEFAULT_MAX_INLINE_CHARS,
            preview_chars: DEFAULT_PREVIEW_CHARS,
        }
    }

    pub fn process(&self, tool_use_id: &str, content: &str) -> String {
        if content.chars().count() <= self.max_inline_chars {
            return content.to_owned();
        }

        if let Err(error) = ensure_tool_results_parent(&self.storage_dir) {
            return format!("{content}\n\n[tool result externalization failed: {error}]");
        }
        if let Err(error) = ensure_private_dir(&self.storage_dir) {
            return format!("{content}\n\n[tool result externalization failed: {error}]");
        }

        let file_path = self.storage_dir.join(result_filename(tool_use_id));
        if let Err(error) = fs::write(&file_path, content) {
            return format!("{content}\n\n[tool result externalization failed: {error}]");
        }
        if let Err(error) = ensure_private_file(&file_path) {
            return format!("{content}\n\n[tool result externalization failed: {error}]");
        }

        let preview = content.chars().take(self.preview_chars).collect::<String>();
        format!(
            "{preview}\n\n... [truncated \u{2014} full output ({} chars) saved to {}]",
            content.chars().count(),
            file_path.display()
        )
    }
}

fn result_filename(tool_use_id: &str) -> String {
    let cleaned = tool_use_id.trim();
    if is_safe_tool_use_id(cleaned) {
        return format!("{cleaned}.txt");
    }
    format!(
        "tool_result_{}.txt",
        blake2b_hex(tool_use_id.as_bytes(), 12)
    )
}

fn is_safe_tool_use_id(value: &str) -> bool {
    !value.is_empty()
        && !matches!(value, "." | "..")
        && !value.contains('/')
        && !value.contains('\\')
        && !value.contains("..")
        && !Path::new(value).is_absolute()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-'))
}

fn ensure_private_dir(path: &Path) -> std::io::Result<()> {
    fs::create_dir_all(path)?;
    restrict_dir_permissions(path)
}

fn ensure_tool_results_parent(storage_dir: &Path) -> std::io::Result<()> {
    let Some(parent) = storage_dir.parent() else {
        return Ok(());
    };
    if parent.file_name().and_then(|name| name.to_str()) == Some("tool-results") {
        ensure_private_dir(parent)?;
    }
    Ok(())
}

fn ensure_private_file(path: &Path) -> std::io::Result<()> {
    restrict_file_permissions(path)
}

#[cfg(unix)]
fn restrict_dir_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_dir_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

const BLAKE2B_IV: [u64; 8] = [
    0x6a09e667f3bcc908,
    0xbb67ae8584caa73b,
    0x3c6ef372fe94f82b,
    0xa54ff53a5f1d36f1,
    0x510e527fade682d1,
    0x9b05688c2b3e6c1f,
    0x1f83d9abfb41bd6b,
    0x5be0cd19137e2179,
];

const BLAKE2B_SIGMA: [[usize; 16]; 12] = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
    [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
    [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
    [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
    [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
    [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
    [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
    [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
    [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
];

fn blake2b_hex(input: &[u8], digest_size: usize) -> String {
    debug_assert!(digest_size <= 64);
    let mut h = BLAKE2B_IV;
    h[0] ^= 0x0101_0000 ^ digest_size as u64;

    let mut offset = 0usize;
    let mut counter = 0u128;
    while input.len().saturating_sub(offset) > 128 {
        let block = &input[offset..offset + 128];
        counter += 128;
        blake2b_compress(&mut h, block, counter, false);
        offset += 128;
    }

    let last = &input[offset..];
    let mut block = [0u8; 128];
    block[..last.len()].copy_from_slice(last);
    counter += last.len() as u128;
    blake2b_compress(&mut h, &block, counter, true);

    let mut output = [0u8; 64];
    for (index, word) in h.iter().enumerate() {
        output[index * 8..index * 8 + 8].copy_from_slice(&word.to_le_bytes());
    }
    hex_lower(&output[..digest_size])
}

fn blake2b_compress(h: &mut [u64; 8], block: &[u8], counter: u128, last: bool) {
    let mut m = [0u64; 16];
    for (index, word) in m.iter_mut().enumerate() {
        let start = index * 8;
        let mut bytes = [0u8; 8];
        bytes.copy_from_slice(&block[start..start + 8]);
        *word = u64::from_le_bytes(bytes);
    }

    let mut v = [0u64; 16];
    v[..8].copy_from_slice(h);
    v[8..].copy_from_slice(&BLAKE2B_IV);
    v[12] ^= counter as u64;
    v[13] ^= (counter >> 64) as u64;
    if last {
        v[14] = !v[14];
    }

    for sigma in BLAKE2B_SIGMA {
        blake2b_g(&mut v, 0, 4, 8, 12, m[sigma[0]], m[sigma[1]]);
        blake2b_g(&mut v, 1, 5, 9, 13, m[sigma[2]], m[sigma[3]]);
        blake2b_g(&mut v, 2, 6, 10, 14, m[sigma[4]], m[sigma[5]]);
        blake2b_g(&mut v, 3, 7, 11, 15, m[sigma[6]], m[sigma[7]]);
        blake2b_g(&mut v, 0, 5, 10, 15, m[sigma[8]], m[sigma[9]]);
        blake2b_g(&mut v, 1, 6, 11, 12, m[sigma[10]], m[sigma[11]]);
        blake2b_g(&mut v, 2, 7, 8, 13, m[sigma[12]], m[sigma[13]]);
        blake2b_g(&mut v, 3, 4, 9, 14, m[sigma[14]], m[sigma[15]]);
    }

    for index in 0..8 {
        h[index] ^= v[index] ^ v[index + 8];
    }
}

fn blake2b_g(v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64) {
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(x);
    v[d] = (v[d] ^ v[a]).rotate_right(32);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(24);
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(y);
    v[d] = (v[d] ^ v[a]).rotate_right(16);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(63);
}

fn hex_lower(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

#[cfg(test)]
mod tests {
    use super::result_filename;

    #[test]
    fn result_filename_preserves_safe_ids_like_python() {
        assert_eq!(result_filename(" toolu_1 "), "toolu_1.txt");
        assert_eq!(result_filename("abc-DEF_123.x"), "abc-DEF_123.x.txt");
    }

    #[test]
    fn result_filename_hashes_unsafe_ids_like_python_blake2b_12() {
        assert_eq!(
            result_filename("bad/../id"),
            "tool_result_867253c715b0f21a2afb5f4b.txt"
        );
        assert_eq!(
            result_filename(".."),
            "tool_result_6f495c9e95710478162e2120.txt"
        );
    }
}
