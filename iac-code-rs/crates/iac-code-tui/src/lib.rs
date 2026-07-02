use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use iac_code_protocol::json::{self, JsonValue};

mod ansi;
mod banner;
mod command_provider;
mod fuzzy_picker;
mod global_search;
mod history_search;
mod keybinding_manager;
mod markdown;
mod model_picker;
mod path_provider;
mod prompt_buffer;
mod prompt_editor;
mod quick_open;
mod resume_picker;
mod selection_window;
mod shell_history_provider;
mod skill_provider;
mod skills_picker;
mod spinner;
mod suggestions;
mod terminal_key;
mod terminal_mode;
mod text_wrap;
mod transcript_reflow;
mod transcript_view;
mod width;

pub use command_provider::{
    CommandCatalog, CommandDefinition, CommandSuggestionProvider, FuzzyMatch,
    MemorySuggestionEntry, MemorySuggestionSource,
};
pub use fuzzy_picker::{FuzzyPickerState, PickerItem};
pub use global_search::{
    build_global_search_preview, parse_global_search_results, GlobalSearchItem, GlobalSearchPreview,
};
pub use history_search::{
    build_history_search_items, HistoryContentBlock, HistoryMessage, HistoryMessageContent,
    HistorySearchItem, HistorySearchState,
};
pub use keybinding_manager::{
    default_global_keybinding_manager, register_default_global_keybindings, KeyBinding,
    KeyBindingId, KeybindingManager,
};
pub use markdown::render_markdown_ansi;
pub use model_picker::{
    EffortLevel, ModelDefinition, ModelPickerEntry, ModelPickerState, ModelProviderGroup,
    ModelSelection, ModelThinkingSpec,
};
pub use path_provider::{fuzzy_match, DirectorySuggestionProvider, FileSuggestionProvider};
pub use prompt_buffer::PromptBuffer;
pub use prompt_editor::{PromptEditOutcome, PromptEditor, PromptKeyEvent};
pub use quick_open::{
    build_quick_open_items, build_quick_open_preview, QuickOpenItem, QuickOpenPreview,
};
pub use resume_picker::{
    format_resume_session_size, short_resume_session_id, ResumePickerState, ResumeSessionEntry,
    WHEEL_LINES,
};
pub use shell_history_provider::{
    detect_shell_history_path, read_shell_history, ShellHistoryProvider, MAX_HISTORY_SUGGESTIONS,
};
pub use skill_provider::{SkillCatalog, SkillDefinition, SkillFuzzyMatch, SkillSuggestionProvider};
pub use skills_picker::{
    SkillManagementItem, SkillManagementSource, SkillsPickerState, SkillsSortMode,
};
pub use spinner::{
    format_spinner_elapsed, spinner_frame_at, ShimmerSpinnerState, COMPLETION_VERBS, SPINNER_COLOR,
    SPINNER_DOTS, SPINNER_VERBS,
};
pub use suggestions::{
    CompletionToken, SuggestionAggregator, SuggestionItem, SuggestionProvider, TokenExtractor,
    OVERLAY_MAX_ITEMS,
};
#[cfg(unix)]
pub use terminal_key::read_terminal_key;
pub use terminal_key::{
    decode_terminal_input, is_bracketed_paste_start, parse_bracketed_paste_bytes,
    parse_terminal_escape_sequence, parse_terminal_key_byte,
};
#[cfg(unix)]
pub use terminal_mode::{
    make_raw_termios, terminal_dimensions, RawInputCapture, RawTerminalModeGuard,
    TerminalDimensions,
};
pub use terminal_mode::{
    terminal_mode_enter_sequences, terminal_mode_exit_sequences,
    write_terminal_mode_enter_sequences, write_terminal_mode_exit_sequences, TerminalModeGuard,
    TERMINAL_MODE_ENTER_SEQUENCES, TERMINAL_MODE_EXIT_SEQUENCES,
};
pub use text_wrap::{wrap_transcript_lines, wrap_transcript_lines_tail};
pub use transcript_reflow::{
    TranscriptReflowState, TranscriptWidthObservation, TRANSCRIPT_REFLOW_DEBOUNCE,
};
pub use transcript_view::{
    draw_transcript_view, draw_transcript_view_wrapped, transcript_should_exit,
    DrawnTranscriptView, TranscriptSegment, TranscriptTurn, TranscriptViewState,
};
pub use width::{
    suffix_start_for_display_width, take_prefix_by_display_width, terminal_display_width,
    truncate_to_display_width, usable_content_width,
};

const HISTORY_FORMAT: &str = "iac-code-input-history-v1";

pub const CRATE_NAME: &str = "iac-code-tui";

pub use banner::{
    format_update_command, render_update_notice_lines, render_update_prompt_header_lines,
    render_welcome_banner_ansi_lines, render_welcome_banner_lines, BannerUpdate,
    WelcomeBannerLabels, WelcomeBannerState, ACCENT, LOGO_LINES,
};

#[derive(Debug)]
pub struct InputHistory {
    path: PathBuf,
    entries: Vec<String>,
    session_only: BTreeSet<usize>,
    nav_index: Option<usize>,
    saved_input: String,
}

impl InputHistory {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let entries = load_entries(&path);
        Self {
            path,
            entries,
            session_only: BTreeSet::new(),
            nav_index: None,
            saved_input: String::new(),
        }
    }

    pub fn append(&mut self, entry: impl Into<String>) -> io::Result<()> {
        self.append_with_persistence(entry.into(), true)
    }

    pub fn append_session_only(&mut self, entry: impl Into<String>) -> io::Result<()> {
        self.append_with_persistence(entry.into(), false)
    }

    pub fn reset_navigation(&mut self) {
        self.nav_index = None;
        self.saved_input.clear();
    }

    pub fn is_navigating(&self) -> bool {
        self.nav_index.is_some()
    }

    pub fn saved_input(&self) -> &str {
        &self.saved_input
    }

    pub fn search(&self, prefix: &str) -> Vec<String> {
        self.entries
            .iter()
            .rev()
            .filter(|entry| entry.starts_with(prefix))
            .cloned()
            .collect()
    }

    pub fn entries(&self) -> Vec<String> {
        self.entries.clone()
    }

    pub fn navigate(&mut self, direction: i32, current_input: &str) -> Option<String> {
        if self.entries.is_empty() {
            return None;
        }
        let newest_index = self.entries.len() - 1;
        if direction < 0 {
            let index = match self.nav_index {
                None => {
                    self.saved_input = current_input.to_owned();
                    newest_index
                }
                Some(index) => index.saturating_sub(1),
            };
            self.nav_index = Some(index);
            return self.entries.get(index).cloned();
        }

        let index = self.nav_index?;
        if index < newest_index {
            let index = index + 1;
            self.nav_index = Some(index);
            self.entries.get(index).cloned()
        } else {
            self.nav_index = None;
            None
        }
    }

    fn append_with_persistence(&mut self, entry: String, persist: bool) -> io::Result<()> {
        self.nav_index = None;
        self.saved_input.clear();
        if entry.is_empty() {
            return Ok(());
        }
        if self.entries.last().is_some_and(|last| last == &entry) {
            return Ok(());
        }

        self.entries.push(entry);
        if !persist {
            self.session_only.insert(self.entries.len() - 1);
            return Ok(());
        }
        self.save()
    }

    fn save(&self) -> io::Result<()> {
        if let Some(parent) = self
            .path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        let mut output = String::new();
        for (index, entry) in self.entries.iter().enumerate() {
            if self.session_only.contains(&index) {
                continue;
            }
            output.push_str(&encode_entry(entry));
            output.push('\n');
        }
        let temporary = self.path.with_extension(format!(
            "{}.tmp",
            self.path
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or("history")
        ));
        fs::write(&temporary, output)?;
        restrict_file_permissions(&temporary)?;
        fs::rename(&temporary, &self.path)?;
        restrict_file_permissions(&self.path)
    }
}

fn load_entries(path: &Path) -> Vec<String> {
    let Ok(text) = fs::read_to_string(path) else {
        return Vec::new();
    };
    text.split('\n')
        .filter(|line| !line.is_empty())
        .map(decode_line)
        .collect()
}

fn decode_line(line: &str) -> String {
    match json::parse(line) {
        Ok(JsonValue::Object(object)) => {
            if matches!(
                object.get("format"),
                Some(JsonValue::String(value)) if value == HISTORY_FORMAT
            ) {
                if let Some(JsonValue::String(text)) = object.get("text") {
                    return text.clone();
                }
            }
            line.to_owned()
        }
        Ok(_) | Err(_) => line.to_owned(),
    }
}

fn encode_entry(entry: &str) -> String {
    json::object([
        ("format", json::string(HISTORY_FORMAT)),
        ("text", json::string(entry)),
    ])
    .to_compact_json()
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> io::Result<()> {
    Ok(())
}
