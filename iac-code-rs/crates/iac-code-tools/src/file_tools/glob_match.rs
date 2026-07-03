pub(super) fn matches_path_glob(relative_path: &str, glob_pattern: &str) -> bool {
    let normalized_path = normalize_glob_path(relative_path);
    let normalized_pattern = normalize_glob_path(glob_pattern);

    if !normalized_pattern.contains('/') {
        return normalized_path
            .rsplit('/')
            .next()
            .is_some_and(|name| glob_segment_matches(name, &normalized_pattern));
    }

    let path_parts = normalized_path
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<&str>>();
    let pattern_parts = normalized_pattern
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<&str>>();
    glob_path_parts_match(&path_parts, &pattern_parts)
}

fn normalize_glob_path(path: &str) -> String {
    let mut normalized = path.replace('\\', "/");
    while let Some(rest) = normalized.strip_prefix("./") {
        normalized = rest.into();
    }
    normalized
}

fn glob_path_parts_match(path_parts: &[&str], pattern_parts: &[&str]) -> bool {
    if pattern_parts.is_empty() {
        return path_parts.is_empty();
    }

    if pattern_parts[0] == "**" {
        return glob_path_parts_match(path_parts, &pattern_parts[1..])
            || (!path_parts.is_empty() && glob_path_parts_match(&path_parts[1..], pattern_parts));
    }

    !path_parts.is_empty()
        && glob_segment_matches(path_parts[0], pattern_parts[0])
        && glob_path_parts_match(&path_parts[1..], &pattern_parts[1..])
}

pub(super) fn glob_segment_matches(name: &str, pattern: &str) -> bool {
    let name = name.chars().collect::<Vec<char>>();
    let pattern = pattern.chars().collect::<Vec<char>>();
    let mut dp = vec![vec![false; name.len() + 1]; pattern.len() + 1];
    dp[0][0] = true;

    for pi in 1..=pattern.len() {
        if pattern[pi - 1] == '*' {
            dp[pi][0] = dp[pi - 1][0];
        }
    }

    for pi in 1..=pattern.len() {
        for ni in 1..=name.len() {
            dp[pi][ni] = match pattern[pi - 1] {
                '*' => dp[pi - 1][ni] || dp[pi][ni - 1],
                '?' => dp[pi - 1][ni - 1],
                literal => literal == name[ni - 1] && dp[pi - 1][ni - 1],
            };
        }
    }

    dp[pattern.len()][name.len()]
}
