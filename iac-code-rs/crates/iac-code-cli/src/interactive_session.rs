use std::path::PathBuf;

use iac_code_config::paths::ConfigPaths;
use iac_code_core::SessionIndex;
use iac_code_exec::HeadlessRunResult;
use iac_code_tools::TaskManager;
use iac_code_tui::InputHistory;

#[cfg(unix)]
use super::raw_effort::raw_effort_picker_context;
#[cfg(unix)]
use super::raw_model_context::raw_model_picker_context;
#[cfg(unix)]
use super::raw_prompt_context::RawPromptActionContext;
#[cfg(unix)]
use super::raw_resume::raw_resume_entries_from_session_entries;
#[cfg(unix)]
use super::raw_skills::raw_skills_picker_context;
#[cfg(unix)]
use super::session_utils::current_git_branch;

pub(super) struct InteractiveSessionState {
    pub(super) resume: String,
    pub(super) continue_session: bool,
    pub(super) exit_code: i32,
    pub(super) turn_count: u32,
    pub(super) token_count: u64,
    pub(super) debug_enabled: bool,
    pub(super) debug_log_path: Option<PathBuf>,
    pub(super) current_session_id: Option<String>,
    pub(super) task_manager: TaskManager,
    pub(super) input_history: Option<InputHistory>,
    pub(super) transcript_lines: Vec<String>,
}

pub(super) fn append_interactive_transcript(
    state: &mut InteractiveSessionState,
    prompt: &str,
    result: &HeadlessRunResult,
) {
    state.transcript_lines.push(format!("❯ {prompt}"));
    for line in result.stdout.trim_end().lines() {
        state.transcript_lines.push(line.to_owned());
    }
}

#[cfg(unix)]
pub(super) fn raw_prompt_action_context(
    paths: Option<&ConfigPaths>,
    cwd: &str,
    state: &InteractiveSessionState,
) -> RawPromptActionContext {
    let (resume_current_project_entries, resume_all_project_entries) = paths
        .map(|paths| {
            let index = SessionIndex::new(paths.subdirs().projects);
            let current = index
                .list_for_cwd(cwd)
                .map(|entries| raw_resume_entries_from_session_entries(&entries))
                .unwrap_or_default();
            let all = index
                .list_all_projects()
                .map(|entries| raw_resume_entries_from_session_entries(&entries))
                .unwrap_or_default();
            (current, all)
        })
        .unwrap_or_default();
    let (model_initial_model, model_provider_groups) = paths
        .map(raw_model_picker_context)
        .unwrap_or_else(|| (String::new(), Vec::new()));
    let (skill_management_items, skill_locked_names) = paths
        .map(|paths| raw_skills_picker_context(paths, cwd))
        .unwrap_or_default();
    let (effort_model, effort_allowed, effort_current) = paths
        .map(raw_effort_picker_context)
        .unwrap_or_else(|| (String::new(), Vec::new(), None));

    RawPromptActionContext {
        transcript_lines: state.transcript_lines.clone(),
        resume_current_project_entries,
        resume_all_project_entries,
        current_session_id: state.current_session_id.clone(),
        current_branch: current_git_branch(cwd),
        model_initial_model,
        model_provider_groups,
        skill_management_items,
        skill_locked_names,
        config_paths: paths.cloned(),
        effort_model,
        effort_allowed,
        effort_current,
    }
}
