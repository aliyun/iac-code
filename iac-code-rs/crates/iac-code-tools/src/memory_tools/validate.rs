use std::path::Path;

use super::INDEX_FILE;

pub(super) fn validate_name(name: &str) -> Result<String, String> {
    let cleaned = name.trim();
    if cleaned.is_empty()
        || cleaned == "."
        || cleaned == ".."
        || cleaned.contains('/')
        || cleaned.contains('\\')
        || cleaned.contains("..")
        || Path::new(cleaned).is_absolute()
        || !cleaned.chars().all(|character| {
            character == '_'
                || character == '.'
                || character == '-'
                || character.is_ascii_alphanumeric()
        })
        || format!("{cleaned}.md").eq_ignore_ascii_case(INDEX_FILE)
    {
        return Err(format!("Invalid memory name: {name:?}"));
    }
    Ok(cleaned.to_owned())
}
