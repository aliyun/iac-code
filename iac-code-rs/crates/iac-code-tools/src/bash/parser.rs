#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct ParsedCommand {
    pub(super) text: String,
    pub(super) argv: Vec<String>,
    pub(super) redirects: Vec<String>,
    pub(super) is_complex: bool,
}

pub(super) fn parse_command(command: &str) -> Vec<ParsedCommand> {
    split_compound_commands(command)
        .into_iter()
        .filter_map(|text| {
            let (argv, redirects) = split_redirects(shell_words(&text));
            if argv.is_empty() {
                return None;
            }
            let is_complex = argv
                .first()
                .is_some_and(|name| ["eval", "exec", "source", "."].contains(&name.as_str()))
                || text.contains("$(")
                || text.contains('`');
            Some(ParsedCommand {
                text,
                argv,
                redirects,
                is_complex,
            })
        })
        .collect()
}

pub(super) fn command_base(command: &ParsedCommand) -> Option<String> {
    command.argv.first().map(|value| basename(value))
}

fn split_compound_commands(command: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let mut chars = command.chars().peekable();
    let mut in_single = false;
    let mut in_double = false;
    let mut escape = false;

    while let Some(character) = chars.next() {
        if escape {
            current.push(character);
            escape = false;
            continue;
        }
        if in_single {
            if character == '\'' {
                in_single = false;
            }
            current.push(character);
            continue;
        }
        if character == '\\' {
            current.push(character);
            escape = true;
            continue;
        }
        if in_double {
            if character == '"' {
                in_double = false;
            }
            current.push(character);
            continue;
        }
        match character {
            '\'' => {
                in_single = true;
                current.push(character);
            }
            '"' => {
                in_double = true;
                current.push(character);
            }
            ';' => push_command_part(&mut parts, &mut current),
            '&' if chars.peek() == Some(&'&') => {
                chars.next();
                push_command_part(&mut parts, &mut current);
            }
            '|' if chars.peek() == Some(&'|') => {
                chars.next();
                push_command_part(&mut parts, &mut current);
            }
            '|' if current.ends_with('>') => current.push(character),
            '|' if chars.peek() == Some(&'&') => {
                chars.next();
                push_command_part(&mut parts, &mut current);
            }
            '|' => push_command_part(&mut parts, &mut current),
            _ => current.push(character),
        }
    }
    push_command_part(&mut parts, &mut current);
    parts
}

fn push_command_part(parts: &mut Vec<String>, current: &mut String) {
    let trimmed = current.trim();
    if !trimmed.is_empty() {
        parts.push(trimmed.into());
    }
    current.clear();
}

fn shell_words(command: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut in_single = false;
    let mut in_double = false;
    let mut escape = false;

    for character in command.chars() {
        if escape {
            current.push(character);
            escape = false;
            continue;
        }
        if in_single {
            if character == '\'' {
                in_single = false;
            } else {
                current.push(character);
            }
            continue;
        }
        if character == '\\' {
            escape = true;
            continue;
        }
        if in_double {
            if character == '"' {
                in_double = false;
            } else {
                current.push(character);
            }
            continue;
        }
        match character {
            '\'' => in_single = true,
            '"' => in_double = true,
            character if character.is_whitespace() => {
                if !current.is_empty() {
                    words.push(std::mem::take(&mut current));
                }
            }
            _ => current.push(character),
        }
    }
    if !current.is_empty() {
        words.push(current);
    }
    words
}

fn split_redirects(words: Vec<String>) -> (Vec<String>, Vec<String>) {
    let mut argv = Vec::new();
    let mut redirects = Vec::new();
    let mut index = 0;
    while index < words.len() {
        let word = &words[index];
        if let Some(operator) = redirect_operator(word) {
            if let Some(target) = words.get(index + 1) {
                redirects.push(format!("{operator} {target}"));
                index += 2;
            } else {
                redirects.push(operator);
                index += 1;
            }
            continue;
        }
        if let Some((operator, target)) = attached_redirect(word) {
            redirects.push(format!("{operator} {target}"));
            index += 1;
            continue;
        }
        argv.push(word.clone());
        index += 1;
    }
    (argv, redirects)
}

fn redirect_operator(word: &str) -> Option<String> {
    let operator = redirect_suffix_after_fd(word);
    matches!(operator, ">" | ">>" | ">|" | "<>" | "<" | "<<" | "<<<").then(|| word.to_owned())
}

fn attached_redirect(word: &str) -> Option<(String, String)> {
    let fd_len = word
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .map(char::len_utf8)
        .sum::<usize>();
    let rest = &word[fd_len..];
    for operator in [">>", ">|", "<>", "<<<", "<<", ">", "<"] {
        if rest.starts_with(operator) && rest.len() > operator.len() {
            return Some((
                format!("{}{}", &word[..fd_len], operator),
                rest[operator.len()..].to_owned(),
            ));
        }
    }
    None
}

pub(super) fn redirect_suffix_after_fd(word: &str) -> &str {
    word.trim_start_matches(|character: char| character.is_ascii_digit())
}

fn basename(path: &str) -> String {
    path.rsplit(['/', '\\']).next().unwrap_or(path).into()
}
