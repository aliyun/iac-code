pub(super) fn normalize_skill_name(name: &str) -> String {
    name.trim_start_matches(['/', '$'])
        .trim()
        .to_ascii_lowercase()
}
