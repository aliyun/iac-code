use std::collections::BTreeMap;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct RuleMatches {
    pub(super) allow: Vec<String>,
    pub(super) deny: Vec<String>,
    pub(super) ask: Vec<String>,
}

pub(super) fn collect_rules(rules_by_source: &BTreeMap<String, Vec<String>>) -> Vec<String> {
    rules_by_source
        .values()
        .flat_map(|rules| rules.iter().cloned())
        .collect()
}

pub(super) fn find_matching_rules(
    command: &str,
    allow_rules: &[String],
    deny_rules: &[String],
    ask_rules: &[String],
) -> RuleMatches {
    let normalized = normalize_command(command);
    RuleMatches {
        allow: allow_rules
            .iter()
            .filter(|rule| rule_matches(rule, command))
            .cloned()
            .collect(),
        deny: deny_rules
            .iter()
            .filter(|rule| rule_matches(rule, &normalized))
            .cloned()
            .collect(),
        ask: ask_rules
            .iter()
            .filter(|rule| rule_matches(rule, &normalized))
            .cloned()
            .collect(),
    }
}

fn rule_matches(rule: &str, command: &str) -> bool {
    let Some(content) = parse_bash_rule(rule) else {
        return false;
    };
    match_rule(&content, command)
}

fn parse_bash_rule(rule: &str) -> Option<String> {
    let trimmed = rule.trim();
    let rest = trimmed.strip_prefix("bash(")?;
    Some(rest.strip_suffix(')')?.to_owned())
}

fn match_rule(rule_content: &str, command: &str) -> bool {
    if rule_content.is_empty() {
        return false;
    }
    let command = command.trim();
    if let Some(prefix) = rule_content.strip_suffix(":*") {
        return command == prefix || command.starts_with(&format!("{prefix} "));
    }
    if rule_content.contains('*') {
        return wildcard_match(command, rule_content);
    }
    command == rule_content
}

fn wildcard_match(command: &str, pattern: &str) -> bool {
    let command = command.chars().collect::<Vec<char>>();
    let pattern = pattern.chars().collect::<Vec<char>>();
    let mut dp = vec![vec![false; command.len() + 1]; pattern.len() + 1];
    dp[0][0] = true;
    for pi in 1..=pattern.len() {
        if pattern[pi - 1] == '*' {
            dp[pi][0] = dp[pi - 1][0];
        }
    }
    for pi in 1..=pattern.len() {
        for ci in 1..=command.len() {
            dp[pi][ci] = if pattern[pi - 1] == '*' {
                dp[pi - 1][ci] || dp[pi][ci - 1]
            } else {
                pattern[pi - 1] == command[ci - 1] && dp[pi - 1][ci - 1]
            };
        }
    }
    dp[pattern.len()][command.len()]
}

pub(super) fn normalize_command(command: &str) -> String {
    let mut rest = command.trim();
    loop {
        let Some((head, tail)) = rest.split_once(char::is_whitespace) else {
            return rest.into();
        };
        if is_env_assignment(head) {
            rest = tail.trim_start();
        } else {
            return rest.into();
        }
    }
}

fn is_env_assignment(token: &str) -> bool {
    let Some((name, value)) = token.split_once('=') else {
        return false;
    };
    if name.is_empty() || value.is_empty() {
        return false;
    }
    name.chars().enumerate().all(|(index, character)| {
        character == '_'
            || character.is_ascii_alphanumeric() && (index > 0 || character.is_ascii_alphabetic())
    })
}
