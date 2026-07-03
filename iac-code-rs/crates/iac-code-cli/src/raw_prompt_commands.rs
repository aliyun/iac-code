fn prompt_command_parts(prompt: &str) -> impl Iterator<Item = &str> {
    prompt
        .strip_prefix('/')
        .unwrap_or(prompt)
        .split_whitespace()
}

fn prompt_is_bare_command(prompt: &str, command_name: &str) -> bool {
    let mut parts = prompt_command_parts(prompt);
    matches!(parts.next(), Some(command) if command.eq_ignore_ascii_case(command_name))
        && parts.next().is_none()
}

pub(super) fn raw_prompt_should_open_resume_picker(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "resume")
}

pub(super) fn raw_prompt_should_open_rename_prompt(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "rename")
}

pub(super) fn raw_prompt_should_open_auth_flow(prompt: &str) -> bool {
    let mut parts = prompt_command_parts(prompt);
    matches!(
        parts.next(),
        Some(command) if command.eq_ignore_ascii_case("auth") || command.eq_ignore_ascii_case("login")
    ) && parts.next().is_none()
}

pub(super) fn raw_prompt_should_open_model_picker(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "model")
}

pub(super) fn raw_prompt_should_open_effort_picker(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "effort")
}

pub(super) fn raw_prompt_should_open_memory_dialog(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "memory")
}

pub(super) fn raw_prompt_should_open_skills_picker(prompt: &str) -> bool {
    prompt_is_bare_command(prompt, "skills")
}
