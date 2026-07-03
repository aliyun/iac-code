use std::path::PathBuf;

use crate::width::terminal_display_width;

mod formatting;
mod panel;

use formatting::{capitalize_username, display_cwd, shell_quote};
use panel::{ansi_style, panel_row, AnsiStyle, Segment};

pub const LOGO_LINES: [&str; 5] = [
    "      ▄▄███▄▄      ",
    "   ▄██████████▄▄   ",
    " ▄█▀████████████▄  ",
    "████████████████████",
    " ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ ",
];

pub const ACCENT: &str = "bright_cyan";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WelcomeBannerLabels {
    pub welcome_back: String,
    pub description: String,
    pub session: String,
    pub debug_mode: String,
    pub log_file: String,
}

impl Default for WelcomeBannerLabels {
    fn default() -> Self {
        Self {
            welcome_back: "Welcome back".to_owned(),
            description: "Your AI-powered Infrastructure as Code assistant".to_owned(),
            session: "Session".to_owned(),
            debug_mode: "Debug mode".to_owned(),
            log_file: "Log file".to_owned(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WelcomeBannerState {
    pub model: String,
    pub cwd: PathBuf,
    pub version: String,
    pub username: String,
    pub provider_display: Option<String>,
    pub home_dir: Option<PathBuf>,
    pub session_id: Option<String>,
    pub session_name: Option<String>,
    pub debug_log_path: Option<PathBuf>,
    pub labels: WelcomeBannerLabels,
}

impl WelcomeBannerState {
    pub fn new(
        model: impl Into<String>,
        cwd: impl Into<PathBuf>,
        version: impl Into<String>,
    ) -> Self {
        Self {
            model: model.into(),
            cwd: cwd.into(),
            version: version.into(),
            username: "User".to_owned(),
            provider_display: None,
            home_dir: std::env::var_os("HOME").map(PathBuf::from),
            session_id: None,
            session_name: None,
            debug_log_path: None,
            labels: WelcomeBannerLabels::default(),
        }
    }

    pub fn with_username(mut self, username: impl Into<String>) -> Self {
        let username = username.into();
        self.username = capitalize_username(&username);
        self
    }

    pub fn with_provider_display(mut self, provider_display: impl Into<String>) -> Self {
        let provider_display = provider_display.into();
        if !provider_display.is_empty() {
            self.provider_display = Some(provider_display);
        }
        self
    }

    pub fn with_home_dir(mut self, home_dir: impl Into<PathBuf>) -> Self {
        self.home_dir = Some(home_dir.into());
        self
    }

    pub fn with_session(
        mut self,
        session_id: impl Into<String>,
        session_name: Option<&str>,
    ) -> Self {
        self.session_id = Some(session_id.into());
        self.session_name = session_name.map(str::to_owned);
        self
    }

    pub fn with_debug_log_path(mut self, debug_log_path: impl Into<PathBuf>) -> Self {
        self.debug_log_path = Some(debug_log_path.into());
        self
    }

    pub fn with_labels(mut self, labels: WelcomeBannerLabels) -> Self {
        self.labels = labels;
        self
    }

    pub fn cwd_display(&self) -> String {
        display_cwd(&self.cwd, self.home_dir.as_deref())
    }

    pub fn model_display(&self) -> Option<String> {
        if self.model.is_empty() {
            return None;
        }
        Some(match &self.provider_display {
            Some(provider) if !provider.is_empty() => format!("{provider} / {}", self.model),
            _ => self.model.clone(),
        })
    }

    pub fn session_display(&self) -> Option<String> {
        match (self.session_id.as_deref(), self.session_name.as_deref()) {
            (Some(session_id), Some(session_name)) if !session_name.is_empty() => Some(format!(
                "{}: {session_name} ({session_id})",
                self.labels.session
            )),
            (Some(session_id), _) => Some(format!("{}: {session_id}", self.labels.session)),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BannerUpdate {
    pub current_version: String,
    pub version: String,
    pub update_command: Vec<String>,
    pub release_notes_url: Option<String>,
}

impl BannerUpdate {
    pub fn new(
        current_version: impl Into<String>,
        version: impl Into<String>,
        update_command: Vec<String>,
    ) -> Self {
        Self {
            current_version: current_version.into(),
            version: version.into(),
            update_command,
            release_notes_url: None,
        }
    }

    pub fn with_release_notes_url(mut self, release_notes_url: impl Into<String>) -> Self {
        self.release_notes_url = Some(release_notes_url.into());
        self
    }
}

pub fn render_welcome_banner_lines(state: &WelcomeBannerState) -> Vec<String> {
    let mut lines = vec![
        String::new(),
        format!("  {} {}!", state.labels.welcome_back, state.username),
        String::new(),
    ];
    lines.extend(LOGO_LINES.iter().map(|line| format!("   {line}")));
    lines.push(format!("  {}", state.labels.description));
    lines.push(String::new());
    lines.push(format!("  iac-code v{}", state.version));
    if let Some(model_display) = state.model_display() {
        lines.push(format!("  {model_display}"));
    }
    lines.push(format!("  {}", state.cwd_display()));
    if let Some(session_display) = state.session_display() {
        lines.push(format!("  {session_display}"));
    }
    if let Some(log_path) = &state.debug_log_path {
        lines.push(String::new());
        lines.push(format!("  {}", state.labels.debug_mode));
        lines.push(format!(
            "  {}: {}",
            state.labels.log_file,
            log_path.display()
        ));
    }
    lines
}

pub fn render_welcome_banner_ansi_lines(
    state: &WelcomeBannerState,
    terminal_width: usize,
) -> Vec<String> {
    let width = terminal_width.max(20);
    let inner_width = width.saturating_sub(2);
    let mut lines = Vec::new();

    lines.push(ansi_style(
        AnsiStyle::Cyan,
        &format!("╭{}╮", "─".repeat(inner_width)),
    ));
    lines.push(panel_row(inner_width, vec![]));
    lines.push(panel_row(
        inner_width,
        vec![
            Segment::plain(" "),
            Segment::styled(
                AnsiStyle::Bold,
                format!("  {} {}!", state.labels.welcome_back, state.username),
            ),
        ],
    ));
    lines.push(panel_row(inner_width, vec![]));

    for (index, line) in LOGO_LINES.iter().enumerate() {
        let logo_text = format!("   {line}");
        let mut segments = vec![
            Segment::plain(" "),
            Segment::styled(AnsiStyle::Cyan, logo_text.clone()),
        ];
        if index == 2 {
            let current_width = 1 + terminal_display_width(&logo_text);
            let description_width = terminal_display_width(&state.labels.description);
            let desired_width = (inner_width * 62 / 100).max(current_width + 5);
            let max_width = inner_width.saturating_sub(description_width);
            let target_width = desired_width.min(max_width).max(current_width + 5);
            segments.push(Segment::plain(
                " ".repeat(target_width.saturating_sub(current_width)),
            ));
            segments.push(Segment::styled(
                AnsiStyle::ItalicWhite,
                state.labels.description.clone(),
            ));
        }
        lines.push(panel_row(inner_width, segments));
    }

    lines.push(panel_row(inner_width, vec![]));
    lines.push(panel_row(
        inner_width,
        vec![
            Segment::plain(" "),
            Segment::styled(AnsiStyle::Dim, format!("  iac-code v{}", state.version)),
        ],
    ));
    if let Some(model_display) = state.model_display() {
        lines.push(panel_row(
            inner_width,
            vec![
                Segment::plain(" "),
                Segment::styled(AnsiStyle::Dim, format!("  {model_display}")),
            ],
        ));
    }
    lines.push(panel_row(
        inner_width,
        vec![
            Segment::plain(" "),
            Segment::styled(AnsiStyle::Dim, format!("  {}", state.cwd_display())),
        ],
    ));
    if let Some(session_display) = state.session_display() {
        lines.push(panel_row(
            inner_width,
            vec![
                Segment::plain(" "),
                Segment::styled(AnsiStyle::Dim, format!("  {session_display}")),
            ],
        ));
    }

    if let Some(log_path) = &state.debug_log_path {
        lines.push(panel_row(inner_width, vec![]));
        lines.push(panel_row(
            inner_width,
            vec![
                Segment::plain(" "),
                Segment::styled(
                    AnsiStyle::BoldYellow,
                    format!("  {}", state.labels.debug_mode),
                ),
            ],
        ));
        lines.push(panel_row(
            inner_width,
            vec![
                Segment::plain(" "),
                Segment::styled(
                    AnsiStyle::DimYellow,
                    format!("  {}: {}", state.labels.log_file, log_path.display()),
                ),
            ],
        ));
    }

    lines.push(ansi_style(
        AnsiStyle::Cyan,
        &format!("╰{}╯", "─".repeat(inner_width)),
    ));
    lines
}

pub fn render_update_prompt_header_lines(update: &BannerUpdate) -> Vec<String> {
    let mut lines = vec![
        format!(
            "Update available! {} -> {}",
            update.current_version, update.version
        ),
        format!(
            "Update command: {}",
            format_update_command(&update.update_command)
        ),
    ];
    if let Some(url) = &update.release_notes_url {
        lines.push(format!("Release notes: {url}"));
    }
    lines
}

pub fn render_update_notice_lines(update: &BannerUpdate) -> Vec<String> {
    let mut lines = vec![
        format!(
            "Update available! {} -> {}",
            update.current_version, update.version
        ),
        format!(
            "Run {} to update.",
            format_update_command(&update.update_command)
        ),
    ];
    if let Some(url) = &update.release_notes_url {
        lines.push(format!("Release notes: {url}"));
    }
    lines
}

pub fn format_update_command(command: &[String]) -> String {
    command
        .iter()
        .map(|part| shell_quote(part))
        .collect::<Vec<_>>()
        .join(" ")
}
