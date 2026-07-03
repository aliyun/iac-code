use super::SESSION_NAME_PATTERN_TEXT;

pub fn validate_session_name(name: &str) -> Result<&str, String> {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return Err(format!(
            "Session name must match {SESSION_NAME_PATTERN_TEXT}"
        ));
    };
    if !first.is_ascii_alphanumeric() {
        return Err(format!(
            "Session name must match {SESSION_NAME_PATTERN_TEXT}"
        ));
    }
    if name.chars().count() > 200 {
        return Err(format!(
            "Session name must match {SESSION_NAME_PATTERN_TEXT}"
        ));
    }
    if chars.any(|character| {
        !(character.is_ascii_alphanumeric()
            || character == '.'
            || character == '_'
            || character == '-')
    }) {
        return Err(format!(
            "Session name must match {SESSION_NAME_PATTERN_TEXT}"
        ));
    }
    Ok(name)
}

pub fn normalize_session_name(name: &str) -> Result<String, String> {
    let trimmed = name.trim();
    validate_session_name(trimmed)?;
    Ok(trimmed.to_owned())
}
