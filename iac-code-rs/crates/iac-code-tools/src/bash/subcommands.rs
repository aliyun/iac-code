use std::path::Path;

const GIT_READONLY_SUBCOMMANDS: &[&str] = &[
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "tag",
    "remote",
    "describe",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "blame",
    "shortlog",
];

pub(super) fn git_readonly(argv: &[String]) -> bool {
    let remaining = git_remaining_argv(argv);
    let Some(sub) = remaining.first() else {
        return false;
    };
    if sub == "stash" && remaining.get(1).is_some_and(|value| value == "list") {
        return true;
    }
    if sub == "config" {
        return remaining
            .iter()
            .skip(1)
            .any(|arg| matches!(arg.as_str(), "--get" | "--list" | "-l"));
    }
    GIT_READONLY_SUBCOMMANDS.contains(&sub.as_str())
}

fn git_remaining_argv(argv: &[String]) -> Vec<String> {
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if matches!(arg.as_str(), "-C" | "--git-dir" | "--work-tree" | "-c")
            && index + 1 < argv.len()
        {
            index += 2;
            continue;
        }
        if arg.starts_with('-') && arg != "--" {
            index += 1;
            continue;
        }
        break;
    }
    argv[index..].to_vec()
}

pub(super) fn package_manager_readonly(argv: &[String]) -> bool {
    if argv.len() < 2 {
        return false;
    }
    let base = basename(&argv[0]);
    let verb = &argv[1];
    (pip_like_base(&base) && ["list", "show", "freeze"].contains(&verb.as_str()))
        || (base == "npm" && ["list", "ls", "info", "view", "outdated"].contains(&verb.as_str()))
        || (base == "yarn" && ["list", "info", "why"].contains(&verb.as_str()))
        || (base == "pnpm" && ["list", "ls"].contains(&verb.as_str()))
        || (base == "cargo" && verb == "metadata")
        || (base == "go" && verb == "list")
        || (base == "gem" && verb == "list")
        || (base == "brew" && ["list", "info"].contains(&verb.as_str()))
        || (base == "uv"
            && argv.len() >= 3
            && argv[1] == "pip"
            && ["list", "show", "freeze"].contains(&argv[2].as_str()))
}

fn pip_like_base(base: &str) -> bool {
    if base == "pip" {
        return true;
    }
    let Some(suffix) = base.strip_prefix("pip") else {
        return false;
    };
    !suffix.is_empty()
        && suffix
            .chars()
            .all(|character| character.is_ascii_digit() || character == '.')
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.into())
}
