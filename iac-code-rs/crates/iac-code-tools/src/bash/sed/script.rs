const SED_HORIZONTAL_SPACE: &[u8] = b" \t\r\x0c\x0b";
const SED_COMMAND_BOUNDARIES: &[u8] = b";\n{}";
const SED_ADDRESS_MODIFIERS: &[u8] = b"IM";

pub(super) fn sed_script_has_danger(script: &str, danger: u8) -> bool {
    let bytes = script.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        index = sed_next_command_start(bytes, index);
        if index >= bytes.len() {
            return false;
        }

        let command_index = sed_skip_command_prefix(bytes, index);
        if command_index >= bytes.len() {
            return false;
        }

        let command = bytes[command_index];
        if is_sed_command_boundary(command) {
            index = command_index + 1;
            continue;
        }
        if command == b'#' {
            index = sed_skip_to_line_end(bytes, command_index);
            continue;
        }
        if command == b'e' {
            if danger == b'e' {
                return true;
            }
            index = sed_skip_to_command_boundary(bytes, command_index + 1);
            continue;
        }
        if matches!(command, b'w' | b'W') {
            if danger == b'w' {
                return true;
            }
            index = sed_skip_to_command_boundary(bytes, command_index + 1);
            continue;
        }
        if matches!(command, b'r' | b'R') {
            index = sed_skip_to_line_end(bytes, command_index + 1);
            continue;
        }
        if command == b's' {
            let (has_flag, next_index) = sed_substitute_has_flag_at(bytes, command_index, danger);
            if has_flag {
                return true;
            }
            index = next_index;
            continue;
        }
        if matches!(command, b'a' | b'i' | b'c') {
            index = sed_skip_text_command(bytes, command_index);
            continue;
        }
        if command == b'y' {
            index = sed_skip_delimited_command(bytes, command_index);
            continue;
        }
        if matches!(command, b':' | b'b' | b't' | b'T' | b'q' | b'Q' | b'l') {
            index = sed_skip_to_command_boundary(bytes, command_index + 1);
            continue;
        }

        index = command_index + 1;
    }
    false
}

pub(super) fn sed_script_read_paths(script: &str) -> Vec<String> {
    let bytes = script.as_bytes();
    let mut paths = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        index = sed_next_command_start(bytes, index);
        if index >= bytes.len() {
            return paths;
        }

        let command_index = sed_skip_command_prefix(bytes, index);
        if command_index >= bytes.len() {
            return paths;
        }

        let command = bytes[command_index];
        if is_sed_command_boundary(command) {
            index = command_index + 1;
            continue;
        }
        if command == b'#' {
            index = sed_skip_to_line_end(bytes, command_index);
            continue;
        }
        if matches!(command, b'r' | b'R') {
            let (read_path, next_index) = sed_read_file_argument(script, command_index);
            if !read_path.is_empty() && read_path != "-" {
                paths.push(read_path);
            }
            index = next_index;
            continue;
        }
        if matches!(command, b'e' | b'w' | b'W') {
            index = sed_skip_to_command_boundary(bytes, command_index + 1);
            continue;
        }
        if command == b's' {
            index = sed_substitute_has_flag_at(bytes, command_index, b'\0').1;
            continue;
        }
        if matches!(command, b'a' | b'i' | b'c') {
            index = sed_skip_text_command(bytes, command_index);
            continue;
        }
        if command == b'y' {
            index = sed_skip_delimited_command(bytes, command_index);
            continue;
        }
        if matches!(command, b':' | b'b' | b't' | b'T' | b'q' | b'Q' | b'l') {
            index = sed_skip_to_command_boundary(bytes, command_index + 1);
            continue;
        }

        index = command_index + 1;
    }
    paths
}

fn sed_next_command_start(bytes: &[u8], start: usize) -> usize {
    let mut index = start;
    while index < bytes.len()
        && (SED_HORIZONTAL_SPACE.contains(&bytes[index]) || is_sed_command_boundary(bytes[index]))
    {
        index += 1;
    }
    index
}

fn sed_skip_command_prefix(bytes: &[u8], start: usize) -> usize {
    let mut index = sed_skip_horizontal_space(bytes, start);
    if let Some(first_address_end) = sed_skip_address(bytes, index) {
        index = sed_skip_horizontal_space(bytes, first_address_end);
        if index < bytes.len() && bytes[index] == b',' {
            let second_start = sed_skip_horizontal_space(bytes, index + 1);
            let second_address_end = sed_skip_address(bytes, second_start).unwrap_or(second_start);
            index = sed_skip_horizontal_space(bytes, second_address_end);
        }
        if index < bytes.len() && bytes[index] == b'!' {
            index = sed_skip_horizontal_space(bytes, index + 1);
        }
        return index;
    }

    if index < bytes.len() && bytes[index] == b'!' {
        return sed_skip_horizontal_space(bytes, index + 1);
    }
    index
}

fn sed_skip_horizontal_space(bytes: &[u8], start: usize) -> usize {
    let mut index = start;
    while index < bytes.len() && SED_HORIZONTAL_SPACE.contains(&bytes[index]) {
        index += 1;
    }
    index
}

fn sed_skip_address(bytes: &[u8], start: usize) -> Option<usize> {
    if start >= bytes.len() {
        return None;
    }

    match bytes[start] {
        b'0'..=b'9' => {
            let mut index = start;
            while index < bytes.len() && bytes[index].is_ascii_digit() {
                index += 1;
            }
            if index < bytes.len() && bytes[index] == b'~' {
                let step_start = index + 1;
                while index + 1 < bytes.len() && bytes[index + 1].is_ascii_digit() {
                    index += 1;
                }
                if index + 1 == step_start {
                    return Some(step_start - 1);
                }
                return Some(index + 1);
            }
            Some(index)
        }
        b'$' => Some(start + 1),
        b'+' | b'~' => {
            let mut index = start + 1;
            if index >= bytes.len() || !bytes[index].is_ascii_digit() {
                return None;
            }
            while index < bytes.len() && bytes[index].is_ascii_digit() {
                index += 1;
            }
            Some(index)
        }
        b'/' => skip_to_unescaped_delimiter(bytes, start + 1, b'/')
            .map(|end| sed_skip_address_modifiers(bytes, end + 1)),
        b'\\' if start + 1 < bytes.len() && bytes[start + 1] != b'\n' => {
            let delimiter = bytes[start + 1];
            if delimiter == b'\\' {
                return None;
            }
            skip_to_unescaped_delimiter(bytes, start + 2, delimiter)
                .map(|end| sed_skip_address_modifiers(bytes, end + 1))
        }
        _ => None,
    }
}

fn sed_skip_address_modifiers(bytes: &[u8], start: usize) -> usize {
    let mut index = start;
    while index < bytes.len() && SED_ADDRESS_MODIFIERS.contains(&bytes[index]) {
        index += 1;
    }
    index
}

fn sed_substitute_has_flag_at(bytes: &[u8], command_index: usize, flag: u8) -> (bool, usize) {
    if command_index + 1 >= bytes.len() {
        return (false, command_index + 1);
    }

    let delimiter = bytes[command_index + 1];
    if matches!(delimiter, b'\\' | b'\n') {
        return (false, command_index + 1);
    }

    let Some(pattern_end) = skip_to_unescaped_delimiter(bytes, command_index + 2, delimiter) else {
        return (
            false,
            sed_skip_to_command_boundary(bytes, command_index + 1),
        );
    };
    let Some(replacement_end) = skip_to_unescaped_delimiter(bytes, pattern_end + 1, delimiter)
    else {
        return (false, sed_skip_to_command_boundary(bytes, pattern_end + 1));
    };

    let mut index = replacement_end + 1;
    while index < bytes.len()
        && !is_sed_command_boundary(bytes[index])
        && !SED_HORIZONTAL_SPACE.contains(&bytes[index])
    {
        if bytes[index] == flag {
            return (true, index + 1);
        }
        index += 1;
    }

    (false, sed_skip_to_command_boundary(bytes, index))
}

fn sed_read_file_argument(script: &str, command_index: usize) -> (String, usize) {
    let bytes = script.as_bytes();
    let start = sed_skip_horizontal_space(bytes, command_index + 1);
    let line_end = sed_find_line_end(bytes, start);
    (
        script[start..line_end].trim().to_owned(),
        sed_after_line_end(bytes, line_end),
    )
}

fn sed_skip_text_command(bytes: &[u8], command_index: usize) -> usize {
    let mut index = command_index + 1;
    while index < bytes.len() && SED_HORIZONTAL_SPACE.contains(&bytes[index]) {
        index += 1;
    }

    let line_end = sed_find_line_end(bytes, index);
    let uses_literal_lines = index < bytes.len()
        && bytes[index] == b'\\'
        && sed_only_horizontal_space(bytes, index + 1, line_end);
    index = sed_after_line_end(bytes, line_end);
    if !uses_literal_lines {
        return index;
    }

    while index < bytes.len() {
        let line_start = index;
        let line_end = sed_find_line_end(bytes, line_start);
        let continues = sed_line_ends_with_unescaped_backslash(bytes, line_start, line_end);
        index = sed_after_line_end(bytes, line_end);
        if !continues {
            break;
        }
    }
    index
}

fn sed_skip_delimited_command(bytes: &[u8], command_index: usize) -> usize {
    if command_index + 1 >= bytes.len() {
        return command_index + 1;
    }

    let delimiter = bytes[command_index + 1];
    if matches!(delimiter, b'\\' | b'\n') {
        return command_index + 1;
    }

    let Some(first_end) = skip_to_unescaped_delimiter(bytes, command_index + 2, delimiter) else {
        return sed_skip_to_command_boundary(bytes, command_index + 1);
    };
    let Some(second_end) = skip_to_unescaped_delimiter(bytes, first_end + 1, delimiter) else {
        return sed_skip_to_command_boundary(bytes, first_end + 1);
    };
    second_end + 1
}

fn sed_skip_to_command_boundary(bytes: &[u8], start: usize) -> usize {
    let mut index = start;
    while index < bytes.len() && !is_sed_command_boundary(bytes[index]) {
        index += 1;
    }
    index
}

fn sed_skip_to_line_end(bytes: &[u8], start: usize) -> usize {
    sed_after_line_end(bytes, sed_find_line_end(bytes, start))
}

fn sed_find_line_end(bytes: &[u8], start: usize) -> usize {
    bytes[start..]
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or(bytes.len(), |offset| start + offset)
}

fn sed_after_line_end(bytes: &[u8], line_end: usize) -> usize {
    if line_end < bytes.len() && bytes[line_end] == b'\n' {
        line_end + 1
    } else {
        line_end
    }
}

fn sed_only_horizontal_space(bytes: &[u8], start: usize, end: usize) -> bool {
    bytes[start..end]
        .iter()
        .all(|byte| SED_HORIZONTAL_SPACE.contains(byte))
}

fn sed_line_ends_with_unescaped_backslash(bytes: &[u8], start: usize, end: usize) -> bool {
    let mut count = 0;
    let mut index = end;
    while index > start && bytes[index - 1] == b'\\' {
        count += 1;
        index -= 1;
    }
    count % 2 == 1
}

fn skip_to_unescaped_delimiter(bytes: &[u8], start: usize, delimiter: u8) -> Option<usize> {
    let mut escaped = false;
    for (offset, byte) in bytes[start..].iter().enumerate() {
        if escaped {
            escaped = false;
            continue;
        }
        if *byte == b'\\' {
            escaped = true;
            continue;
        }
        if *byte == delimiter {
            return Some(start + offset);
        }
    }
    None
}

fn is_sed_command_boundary(byte: u8) -> bool {
    SED_COMMAND_BOUNDARIES.contains(&byte)
}
