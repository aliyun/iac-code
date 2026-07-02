use super::style::{DIM, RESET};

pub(super) fn render_code_block_lines(code: &str) -> Vec<String> {
    let mut lines = Vec::new();
    for line in code.lines() {
        lines.push(format!("{DIM}    {line}{RESET}"));
    }
    if code.ends_with('\n') && code.lines().next().is_none() {
        lines.push(format!("{DIM}    {RESET}"));
    }
    lines
}
