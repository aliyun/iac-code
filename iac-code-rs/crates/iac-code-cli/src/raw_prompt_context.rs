use std::collections::BTreeSet;

use iac_code_config::paths::ConfigPaths;
use iac_code_tui::{EffortLevel, ModelProviderGroup, SkillManagementItem};

use super::raw_resume::RawResumeSessionEntry;

#[derive(Clone, Debug, Default, PartialEq)]
pub(super) struct RawPromptActionContext {
    pub(super) transcript_lines: Vec<String>,
    pub(super) resume_current_project_entries: Vec<RawResumeSessionEntry>,
    pub(super) resume_all_project_entries: Vec<RawResumeSessionEntry>,
    pub(super) current_session_id: Option<String>,
    pub(super) current_branch: Option<String>,
    pub(super) model_initial_model: String,
    pub(super) model_provider_groups: Vec<ModelProviderGroup>,
    pub(super) skill_management_items: Vec<SkillManagementItem>,
    pub(super) skill_locked_names: BTreeSet<String>,
    pub(super) config_paths: Option<ConfigPaths>,
    pub(super) effort_model: String,
    pub(super) effort_allowed: Vec<EffortLevel>,
    pub(super) effort_current: Option<EffortLevel>,
}
