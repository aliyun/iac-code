use super::hash::blake2b_hex;

const MAX_SANITIZED_LENGTH: usize = 200;

pub fn sanitize_path(name: &str) -> String {
    let sanitized = name
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '-'
            }
        })
        .collect::<String>();
    if sanitized.chars().count() <= MAX_SANITIZED_LENGTH {
        return sanitized;
    }
    let prefix = sanitized
        .chars()
        .take(MAX_SANITIZED_LENGTH)
        .collect::<String>();
    format!("{prefix}-{}", blake2b_hex(name.as_bytes(), 6))
}
