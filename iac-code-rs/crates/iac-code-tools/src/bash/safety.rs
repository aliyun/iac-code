pub(super) fn command_safety_check(command: &str) -> bool {
    !command.contains('\0') && !has_disallowed_control_chars(command) && quotes_balanced(command)
}

fn has_disallowed_control_chars(command: &str) -> bool {
    command.chars().any(|character| {
        let value = character as u32;
        value <= 8 || (14..=31).contains(&value) || value == 127
    })
}

fn quotes_balanced(command: &str) -> bool {
    let mut in_single = false;
    let mut in_double = false;
    let mut escape = false;
    for character in command.chars() {
        if escape {
            escape = false;
            continue;
        }
        if character == '\\' {
            escape = true;
            continue;
        }
        if in_single {
            if character == '\'' {
                in_single = false;
            }
            continue;
        }
        if in_double {
            if character == '"' {
                in_double = false;
            }
            continue;
        }
        if character == '\'' {
            in_single = true;
        } else if character == '"' {
            in_double = true;
        }
    }
    !in_single && !in_double
}
