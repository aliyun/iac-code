pub(super) fn extract_text_from_html(html: &str) -> String {
    let without_scripts = remove_tag_blocks(html, "script");
    let without_styles = remove_tag_blocks(&without_scripts, "style");
    let without_tags = strip_tags(&without_styles);
    collapse_whitespace(&decode_html_entities(&without_tags))
}

fn remove_tag_blocks(input: &str, tag_name: &str) -> String {
    let mut output = String::new();
    let mut rest = input;
    let open_marker = format!("<{tag_name}");
    let close_marker = format!("</{tag_name}>");

    loop {
        let lower = rest.to_ascii_lowercase();
        let Some(start) = lower.find(&open_marker) else {
            output.push_str(rest);
            break;
        };
        output.push_str(&rest[..start]);
        let after_open = &rest[start..];
        let lower_after_open = after_open.to_ascii_lowercase();
        let Some(end) = lower_after_open.find(&close_marker) else {
            break;
        };
        rest = &after_open[end + close_marker.len()..];
    }
    output
}

fn strip_tags(input: &str) -> String {
    let mut output = String::new();
    let mut in_tag = false;
    for ch in input.chars() {
        match ch {
            '<' => {
                in_tag = true;
                output.push(' ');
            }
            '>' => in_tag = false,
            _ if !in_tag => output.push(ch),
            _ => {}
        }
    }
    output
}

fn decode_html_entities(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    let mut rest = input;
    while let Some(start) = rest.find('&') {
        output.push_str(&rest[..start]);
        let entity_start = &rest[start..];
        let Some(end) = entity_start.find(';') else {
            output.push_str(entity_start);
            return output;
        };
        let entity = &entity_start[1..end];
        if let Some(decoded) = decode_html_entity(entity) {
            output.push(decoded);
        } else {
            output.push_str(&entity_start[..=end]);
        }
        rest = &entity_start[end + 1..];
    }
    output.push_str(rest);
    output
}

fn decode_html_entity(entity: &str) -> Option<char> {
    match entity {
        "amp" => Some('&'),
        "lt" => Some('<'),
        "gt" => Some('>'),
        "quot" => Some('"'),
        "apos" | "#39" => Some('\''),
        "nbsp" => Some('\u{00a0}'),
        _ => {
            let value = entity
                .strip_prefix("#x")
                .or_else(|| entity.strip_prefix("#X"))
                .and_then(|digits| u32::from_str_radix(digits, 16).ok())
                .or_else(|| {
                    entity
                        .strip_prefix('#')
                        .and_then(|digits| digits.parse::<u32>().ok())
                })?;
            char::from_u32(value)
        }
    }
}

fn collapse_whitespace(input: &str) -> String {
    input.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn html_extraction_decodes_numeric_entities_like_python_html_unescape() {
        let output = extract_text_from_html("<p>ROS &#65;&#x42; &amp; Terraform</p>");

        assert_eq!(output, "ROS AB & Terraform");
    }
}
