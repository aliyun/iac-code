use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub fn terminal_display_width(text: &str) -> usize {
    UnicodeWidthStr::width(text)
}

pub fn usable_content_width(total_width: usize, reserved_cols: usize) -> Option<usize> {
    total_width
        .checked_sub(reserved_cols)
        .filter(|remaining| *remaining > 0)
}

pub fn truncate_to_display_width(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }

    let mut used = 0usize;
    let mut out = String::new();
    for grapheme in text.graphemes(true) {
        let grapheme_width = terminal_display_width(grapheme);
        if used.saturating_add(grapheme_width) > width {
            break;
        }
        used += grapheme_width;
        out.push_str(grapheme);
    }
    out
}

pub fn take_prefix_by_display_width(text: &str, width: usize) -> (&str, &str) {
    if width == 0 || text.is_empty() {
        return ("", text);
    }

    let mut used = 0usize;
    let mut end = 0usize;
    for (index, grapheme) in text.grapheme_indices(true) {
        let grapheme_width = terminal_display_width(grapheme);
        if used.saturating_add(grapheme_width) > width {
            break;
        }
        used += grapheme_width;
        end = index + grapheme.len();
        if used == width {
            break;
        }
    }

    (&text[..end], &text[end..])
}

pub fn suffix_start_for_display_width(text: &str, width: usize) -> usize {
    let mut used = 0usize;
    let mut start = text.len();
    for (index, grapheme) in text.grapheme_indices(true).rev() {
        let grapheme_width = terminal_display_width(grapheme);
        if grapheme_width > 0 && used.saturating_add(grapheme_width) > width {
            break;
        }
        used = used.saturating_add(grapheme_width);
        start = index;
    }
    start
}

pub(crate) fn take_first_grapheme(text: &str) -> (&str, &str) {
    if let Some((index, grapheme)) = text.grapheme_indices(true).next() {
        let end = index + grapheme.len();
        (&text[..end], &text[end..])
    } else {
        ("", "")
    }
}
