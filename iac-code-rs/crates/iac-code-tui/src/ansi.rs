use crate::width::terminal_display_width;

pub(crate) fn strip_ansi_sequences(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(character) = chars.next() {
        if character == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for next in chars.by_ref() {
                if next.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            output.push(character);
        }
    }
    output
}

pub(crate) fn ansi_display_width(input: &str) -> usize {
    terminal_display_width(&strip_ansi_sequences(input))
}
