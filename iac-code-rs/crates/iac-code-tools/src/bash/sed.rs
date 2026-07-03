mod script;

use self::script::{sed_script_has_danger, sed_script_read_paths};

pub(super) fn sed_inplace_edit(argv: &[String]) -> bool {
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if arg == "--" {
            break;
        }
        if arg.starts_with("--in-place") || arg == "-i" || (arg.len() > 2 && arg.starts_with("-i"))
        {
            return true;
        }
        if !arg.starts_with('-') || arg.starts_with("--") {
            index += 1;
            continue;
        }
        if matches!(arg.as_str(), "-e" | "-f") {
            index += 2;
            continue;
        }
        if arg.starts_with("-e") || arg.starts_with("-f") {
            index += 1;
            continue;
        }
        if arg[1..].contains('i') {
            return true;
        }
        index += 1;
    }
    false
}

pub(super) fn sed_uses_script_file(argv: &[String]) -> bool {
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if arg == "--" {
            break;
        }
        if matches!(arg.as_str(), "-e" | "--expression") {
            index += 2;
            continue;
        }
        if arg.starts_with("--expression=") {
            index += 1;
            continue;
        }
        if arg.starts_with("-e") && !arg.starts_with("--") && arg.len() > 2 {
            index += 1;
            continue;
        }
        if matches!(arg.as_str(), "-f" | "--file") || arg.starts_with("--file=") {
            return true;
        }
        if arg.starts_with('-') && !arg.starts_with("--") && arg[1..].contains('f') {
            return true;
        }
        if !arg.starts_with('-') || arg == "-" {
            break;
        }
        index += 1;
    }
    false
}

pub(super) fn sed_executes_shell(argv: &[String]) -> bool {
    sed_script_args(argv)
        .into_iter()
        .any(|script| sed_script_has_danger(script, b'e'))
}

pub(super) fn sed_writes_file(argv: &[String]) -> bool {
    sed_script_args(argv)
        .into_iter()
        .any(|script| sed_script_has_danger(script, b'w'))
}

fn sed_script_args(argv: &[String]) -> Vec<&str> {
    let mut scripts = Vec::new();
    let mut has_script_option = false;
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if arg == "--" {
            if !has_script_option {
                if let Some(script) = argv.get(index + 1) {
                    scripts.push(script.as_str());
                }
            }
            break;
        }

        if matches!(arg.as_str(), "-e" | "--expression") {
            has_script_option = true;
            if let Some(script) = argv.get(index + 1) {
                scripts.push(script.as_str());
            }
            index += 2;
            continue;
        }
        if let Some(script) = arg.strip_prefix("--expression=") {
            has_script_option = true;
            scripts.push(script);
            index += 1;
            continue;
        }
        if arg.starts_with("-e") && !arg.starts_with("--") && arg.len() > 2 {
            has_script_option = true;
            scripts.push(&arg[2..]);
            index += 1;
            continue;
        }
        if let Some(attached_script) = sed_attached_short_expression(arg) {
            has_script_option = true;
            scripts.push(attached_script);
            index += 1;
            continue;
        }

        if matches!(arg.as_str(), "-f" | "--file") {
            has_script_option = true;
            index += 2;
            continue;
        }
        if arg.starts_with("--file=")
            || (arg.starts_with("-f") && !arg.starts_with("--") && arg.len() > 2)
        {
            has_script_option = true;
            index += 1;
            continue;
        }

        if arg.starts_with('-') && arg != "-" {
            index += 1;
            continue;
        }

        if !has_script_option {
            scripts.push(arg.as_str());
        }
        break;
    }
    scripts
}

fn sed_attached_short_expression(arg: &str) -> Option<&str> {
    if !arg.starts_with('-') || arg.starts_with("--") || arg.len() <= 2 {
        return None;
    }
    let option_body = &arg[1..];
    let expression_index = option_body.find('e')?;
    if expression_index == option_body.len() - 1 {
        return None;
    }
    if option_body
        .find('f')
        .is_some_and(|file_index| file_index < expression_index)
    {
        return None;
    }
    Some(&option_body[expression_index + 1..])
}

pub(super) fn sed_read_paths(argv: &[String]) -> Vec<String> {
    let mut script_from_option = false;
    let mut positionals = Vec::new();
    let mut paths = Vec::new();
    let mut script_read_paths = Vec::new();
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if arg == "--" {
            positionals.extend(argv[index + 1..].iter().cloned());
            break;
        }

        if let Some(script) = arg.strip_prefix("--expression=") {
            script_from_option = true;
            script_read_paths.extend(sed_script_read_paths(script));
            index += 1;
            continue;
        }
        if let Some(path) = arg.strip_prefix("--file=") {
            script_from_option = true;
            paths.push(path.to_owned());
            index += 1;
            continue;
        }

        if matches!(arg.as_str(), "-e" | "--expression") {
            script_from_option = true;
            if let Some(script) = argv.get(index + 1) {
                script_read_paths.extend(sed_script_read_paths(script));
            }
            index += 2;
            continue;
        }
        if matches!(arg.as_str(), "-f" | "--file") {
            script_from_option = true;
            if let Some(path) = argv.get(index + 1) {
                paths.push(path.clone());
            }
            index += 2;
            continue;
        }
        if arg.starts_with("-e") && !arg.starts_with("--") && arg.len() > 2 {
            script_from_option = true;
            script_read_paths.extend(sed_script_read_paths(&arg[2..]));
            index += 1;
            continue;
        }
        if let Some(attached_script) = sed_attached_short_expression(arg) {
            script_from_option = true;
            script_read_paths.extend(sed_script_read_paths(attached_script));
            index += 1;
            continue;
        }
        if arg.starts_with("-f") && !arg.starts_with("--") && arg.len() > 2 {
            script_from_option = true;
            paths.push(arg[2..].to_owned());
            index += 1;
            continue;
        }

        if arg.starts_with('-') && arg != "-" {
            index += 1;
            continue;
        }

        positionals.push(arg.clone());
        index += 1;
    }

    if !script_from_option {
        if let Some(script) = positionals.first() {
            script_read_paths.extend(sed_script_read_paths(script));
        }
    }

    paths.extend(script_read_paths);
    if script_from_option {
        paths.extend(positionals);
    } else {
        paths.extend(positionals.into_iter().skip(1));
    }
    paths
}
