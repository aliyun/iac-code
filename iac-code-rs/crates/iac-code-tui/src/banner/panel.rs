use crate::width::{terminal_display_width, truncate_to_display_width};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum AnsiStyle {
    Plain,
    Cyan,
    Bold,
    ItalicWhite,
    Dim,
    BoldYellow,
    DimYellow,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct Segment {
    style: AnsiStyle,
    text: String,
}

impl Segment {
    pub(super) fn plain(text: impl Into<String>) -> Self {
        Self {
            style: AnsiStyle::Plain,
            text: text.into(),
        }
    }

    pub(super) fn styled(style: AnsiStyle, text: impl Into<String>) -> Self {
        Self {
            style,
            text: text.into(),
        }
    }
}

pub(super) fn panel_row(inner_width: usize, segments: Vec<Segment>) -> String {
    let mut visible = 0usize;
    let mut content = String::new();

    for segment in segments {
        if visible >= inner_width {
            break;
        }
        let remaining = inner_width - visible;
        let segment_width = terminal_display_width(&segment.text);
        let text = if segment_width > remaining {
            truncate_to_display_width(&segment.text, remaining)
        } else {
            segment.text
        };
        visible += terminal_display_width(&text);
        content.push_str(&ansi_style(segment.style, &text));
    }

    let padding = inner_width.saturating_sub(visible);
    format!(
        "{}{}{}{}",
        ansi_style(AnsiStyle::Cyan, "│"),
        content,
        " ".repeat(padding),
        ansi_style(AnsiStyle::Cyan, "│")
    )
}

pub(super) fn ansi_style(style: AnsiStyle, text: &str) -> String {
    match style {
        AnsiStyle::Plain => text.to_owned(),
        AnsiStyle::Cyan => format!("\x1b[96m{text}\x1b[0m"),
        AnsiStyle::Bold => format!("\x1b[1m{text}\x1b[0m"),
        AnsiStyle::ItalicWhite => format!("\x1b[3;37m{text}\x1b[0m"),
        AnsiStyle::Dim => format!("\x1b[2m{text}\x1b[0m"),
        AnsiStyle::BoldYellow => format!("\x1b[1;33m{text}\x1b[0m"),
        AnsiStyle::DimYellow => format!("\x1b[2;33m{text}\x1b[0m"),
    }
}
