use std::env;
use std::fs;
use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;
use std::path::PathBuf;

use iac_code_config::paths::ConfigPaths;
use iac_code_core::sanitize_path;
use iac_code_tui::terminal_display_width;
#[cfg(unix)]
use iac_code_tui::RawInputCapture;

use super::cli_i18n::tr;
#[cfg(unix)]
use super::raw_picker::{
    clear_raw_picker, raw_picker_clear_sequence, raw_picker_push_line, raw_picker_terminal_width,
    write_raw_interactive_fd_all,
};
#[cfg(unix)]
use super::raw_prompt_context::RawPromptActionContext;
use super::session_utils::{current_working_directory, find_git_worktree_root};
use super::yaml_config::yaml_mapping_get;

const DEFAULT_INSTRUCTION_MEMORY_FILE: &str = "AGENTS.md";
const INSTRUCTION_MEMORY_FILE_ENV: &str = "IAC_CODE_INSTRUCTION_MEMORY_FILE";

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum RawMemoryAction {
    Project(PathBuf),
    User(PathBuf),
    Folder(PathBuf),
}

#[cfg(unix)]
pub(super) fn read_raw_memory_dialog(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<RawMemoryAction>> {
    let Some(paths) = &context.config_paths else {
        return Ok(None);
    };
    let cwd = current_working_directory().map_err(io::Error::other)?;
    let runtime = memory_runtime_paths(paths, &cwd);
    let mut auto_memory_enabled = is_auto_memory_enabled(paths);
    let mut focused = 0usize;
    let mut rendered_lines = 0usize;
    rendered_lines =
        render_raw_memory_dialog(fd, rendered_lines, &runtime, auto_memory_enabled, focused)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        let max_focus = memory_dialog_options(&runtime, auto_memory_enabled).len();
        if key == "enter" {
            if focused == 0 {
                auto_memory_enabled = !auto_memory_enabled;
                save_auto_memory_enabled(paths, auto_memory_enabled)?;
                focused = focused.min(memory_dialog_options(&runtime, auto_memory_enabled).len());
                rendered_lines = render_raw_memory_dialog(
                    fd,
                    rendered_lines,
                    &runtime,
                    auto_memory_enabled,
                    focused,
                )?;
                continue;
            }
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(match focused {
                1 => Some(RawMemoryAction::Project(runtime.project_path.clone())),
                2 => Some(RawMemoryAction::User(runtime.user_path.clone())),
                3 if auto_memory_enabled => {
                    fs::create_dir_all(&runtime.auto_memory_dir)?;
                    Some(RawMemoryAction::Folder(runtime.auto_memory_dir.clone()))
                }
                _ => None,
            });
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            focused = focused.saturating_sub(1);
            rendered_lines = render_raw_memory_dialog(
                fd,
                rendered_lines,
                &runtime,
                auto_memory_enabled,
                focused,
            )?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            focused = (focused + 1).min(max_focus);
            rendered_lines = render_raw_memory_dialog(
                fd,
                rendered_lines,
                &runtime,
                auto_memory_enabled,
                focused,
            )?;
        }
    }
}

#[cfg(unix)]
fn render_raw_memory_dialog(
    fd: RawFd,
    previous_lines: usize,
    runtime: &MemoryRuntimePaths,
    auto_memory_enabled: bool,
    focused: usize,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) = raw_memory_dialog_render_output(
        previous_lines,
        runtime,
        auto_memory_enabled,
        focused,
        width,
    );
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn raw_memory_dialog_render_output(
    previous_lines: usize,
    runtime: &MemoryRuntimePaths,
    auto_memory_enabled: bool,
    focused: usize,
    width: usize,
) -> (String, usize) {
    let summary = format_memory_dialog_summary(runtime, auto_memory_enabled, focused);
    let lines = summary.lines().collect::<Vec<_>>();
    let mut output = raw_picker_clear_sequence(previous_lines);
    for line in &lines {
        raw_picker_push_line(&mut output, line, width);
    }
    (output, lines.len())
}

#[cfg(unix)]
pub(super) fn raw_memory_action_message(action: &RawMemoryAction) -> String {
    match action {
        RawMemoryAction::Project(path) => {
            format!("{}: {}", tr("Project memory"), path.display())
        }
        RawMemoryAction::User(path) => format!("{}: {}", tr("User memory"), path.display()),
        RawMemoryAction::Folder(path) => {
            format!("{}: {}", tr("Open auto-memory folder"), path.display())
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct MemoryRuntimePaths {
    project_path: PathBuf,
    user_path: PathBuf,
    auto_memory_dir: PathBuf,
}

pub(super) fn memory_runtime_paths(paths: &ConfigPaths, cwd: &str) -> MemoryRuntimePaths {
    let instruction_file = instruction_memory_file_name();
    let project_root = find_git_worktree_root(cwd)
        .or_else(|| PathBuf::from(cwd).canonicalize().ok())
        .unwrap_or_else(|| PathBuf::from(cwd));
    let project_key = sanitize_path(&project_root.to_string_lossy());
    MemoryRuntimePaths {
        project_path: project_root.join(&instruction_file),
        user_path: paths.config_dir.join(&instruction_file),
        auto_memory_dir: paths.subdirs().projects.join(project_key).join("memory"),
    }
}

pub(super) fn instruction_memory_file_name() -> String {
    let configured = env::var(INSTRUCTION_MEMORY_FILE_ENV).unwrap_or_default();
    let trimmed = configured.trim();
    if trimmed.is_empty()
        || trimmed == "."
        || trimmed == ".."
        || trimmed.contains('/')
        || trimmed.contains('\\')
    {
        return DEFAULT_INSTRUCTION_MEMORY_FILE.to_owned();
    }
    trimmed.to_owned()
}

pub(super) fn is_auto_memory_enabled(paths: &ConfigPaths) -> bool {
    let Ok(content) = fs::read_to_string(&paths.settings_path) else {
        return true;
    };
    let Ok(value) = serde_yaml::from_str::<serde_yaml::Value>(&content) else {
        return true;
    };
    let Some(root) = value.as_mapping() else {
        return true;
    };
    yaml_mapping_get(root, "memory")
        .and_then(serde_yaml::Value::as_mapping)
        .and_then(|memory| yaml_mapping_get(memory, "autoMemory"))
        .and_then(serde_yaml::Value::as_bool)
        .unwrap_or(true)
}

#[cfg(unix)]
fn save_auto_memory_enabled(paths: &ConfigPaths, enabled: bool) -> io::Result<()> {
    let existing = match fs::read_to_string(&paths.settings_path) {
        Ok(content) => content,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(error),
    };
    let mut content = remove_root_yaml_block_local(&existing, "memory");
    if !content.is_empty() && !content.ends_with('\n') {
        content.push('\n');
    }
    content.push_str("memory:\n  autoMemory: ");
    content.push_str(if enabled { "true" } else { "false" });
    content.push('\n');
    if let Some(parent) = paths.settings_path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&paths.settings_path, content)
}

#[cfg(unix)]
fn remove_root_yaml_block_local(content: &str, key: &str) -> String {
    let lines = content.lines().map(str::to_owned).collect::<Vec<_>>();
    let Some(start) = lines
        .iter()
        .position(|line| line.trim_start() == format!("{key}:"))
    else {
        return content.to_owned();
    };
    if !lines[start].starts_with(key) {
        return content.to_owned();
    }
    let end = lines
        .iter()
        .enumerate()
        .skip(start + 1)
        .find_map(|(index, line)| {
            (!line.trim().is_empty() && !line.starts_with(' ') && !line.starts_with('\t'))
                .then_some(index)
        })
        .unwrap_or(lines.len());
    let mut kept = lines[..start].to_vec();
    kept.extend_from_slice(&lines[end..]);
    if kept.is_empty() {
        String::new()
    } else {
        let mut output = kept.join("\n");
        output.push('\n');
        output
    }
}

pub(super) fn format_memory_dialog_summary(
    runtime: &MemoryRuntimePaths,
    auto_memory_enabled: bool,
    focused_index: usize,
) -> String {
    let mut lines = vec![format!("  {}", tr("Memory")), String::new()];
    lines.push(format_memory_dialog_row(
        &tr("Auto-memory: {state}").replace(
            "{state}",
            &if auto_memory_enabled {
                tr("on")
            } else {
                tr("off")
            },
        ),
        focused_index == 0,
    ));
    lines.push(String::new());
    let options = memory_dialog_options(runtime, auto_memory_enabled);
    for (index, (label, description)) in options.iter().enumerate() {
        let numbered = format!("{}. {}", index + 1, label);
        let padding = " ".repeat(memory_dialog_option_padding(&numbered));
        lines.push(format_memory_dialog_row(
            &format!("{numbered}{padding}{description}"),
            focused_index == index + 1,
        ));
    }
    lines.push(String::new());
    lines.push(format!("  {}", tr("Enter to confirm · Esc to cancel")));
    lines.join("\n")
}

fn memory_dialog_options(
    runtime: &MemoryRuntimePaths,
    auto_memory_enabled: bool,
) -> Vec<(String, String)> {
    let mut options = vec![
        (
            tr("Project memory"),
            tr("Saved in {path}").replace("{path}", &runtime.project_path.display().to_string()),
        ),
        (
            tr("User memory"),
            tr("Saved in {path}").replace("{path}", &runtime.user_path.display().to_string()),
        ),
    ];
    if auto_memory_enabled {
        options.push((
            tr("Open auto-memory folder"),
            runtime.auto_memory_dir.display().to_string(),
        ));
    }
    options
}

fn format_memory_dialog_row(content: &str, focused: bool) -> String {
    format!("  {} {content}", if focused { "❯" } else { " " })
}

fn memory_dialog_option_padding(label: &str) -> usize {
    28usize.saturating_sub(terminal_display_width(label)).max(2)
}
