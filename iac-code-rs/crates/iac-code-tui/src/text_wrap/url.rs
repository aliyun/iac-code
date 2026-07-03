use crate::width::{take_first_grapheme, take_prefix_by_display_width, terminal_display_width};

pub(super) fn wrap_url_aware_line(line: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }

    let mut lines = Vec::new();
    let mut current = String::new();
    for token in line.split_whitespace() {
        if current.is_empty() {
            start_url_aware_line(token, width, &mut current, &mut lines);
            continue;
        }

        let candidate_width = terminal_display_width(&current) + 1 + terminal_display_width(token);
        if candidate_width <= width {
            current.push(' ');
            current.push_str(token);
        } else {
            lines.push(std::mem::take(&mut current));
            start_url_aware_line(token, width, &mut current, &mut lines);
        }
    }

    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn start_url_aware_line(token: &str, width: usize, current: &mut String, lines: &mut Vec<String>) {
    if is_url_like_token(token) || terminal_display_width(token) <= width {
        current.push_str(token);
        return;
    }

    let mut remaining = token;
    while !remaining.is_empty() {
        let (chunk, rest) = take_prefix_by_display_width(remaining, width);
        if chunk.is_empty() {
            let (fallback, fallback_rest) = take_first_grapheme(remaining);
            lines.push(fallback.to_owned());
            remaining = fallback_rest;
            continue;
        }

        if rest.is_empty() {
            current.push_str(chunk);
            return;
        }

        lines.push(chunk.to_owned());
        remaining = rest;
    }
}

pub(super) fn text_contains_url_like(text: &str) -> bool {
    text.split_whitespace().any(is_url_like_token)
}

fn is_url_like_token(raw_token: &str) -> bool {
    let token = trim_url_token(raw_token);
    !token.is_empty() && (is_absolute_url_like(token) || is_bare_url_like(token))
}

fn trim_url_token(token: &str) -> &str {
    token.trim_matches(|ch: char| {
        matches!(
            ch,
            '(' | ')'
                | '['
                | ']'
                | '{'
                | '}'
                | '<'
                | '>'
                | ','
                | '.'
                | ';'
                | ':'
                | '!'
                | '\''
                | '"'
        )
    })
}

fn is_absolute_url_like(token: &str) -> bool {
    let Some((scheme, rest)) = token.split_once("://") else {
        return false;
    };
    if !is_valid_url_scheme(scheme) {
        return false;
    }

    rest.split(['/', '?', '#'])
        .next()
        .is_some_and(|host| !host.is_empty() && !host.chars().any(char::is_whitespace))
}

fn is_valid_url_scheme(scheme: &str) -> bool {
    let mut chars = scheme.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    first.is_ascii_alphabetic()
        && chars.all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '-' | '.'))
}

fn is_bare_url_like(token: &str) -> bool {
    let (host_port, has_url_trailer) = split_host_port_and_trailer(token);
    if host_port.is_empty() {
        return false;
    }
    if !has_url_trailer && !host_port.to_ascii_lowercase().starts_with("www.") {
        return false;
    }

    let (host, port) = split_host_and_port(host_port);
    if host.is_empty() {
        return false;
    }
    if let Some(port) = port {
        if !is_valid_port(port) {
            return false;
        }
    }

    host.eq_ignore_ascii_case("localhost") || is_ipv4(host) || is_domain_name(host)
}

fn split_host_port_and_trailer(token: &str) -> (&str, bool) {
    token
        .find(['/', '?', '#'])
        .map_or((token, false), |index| (&token[..index], true))
}

fn split_host_and_port(host_port: &str) -> (&str, Option<&str>) {
    if let Some((host, port)) = host_port.rsplit_once(':') {
        if !host.is_empty() && !port.is_empty() && port.chars().all(|ch| ch.is_ascii_digit()) {
            return (host, Some(port));
        }
    }

    (host_port, None)
}

fn is_valid_port(port: &str) -> bool {
    !port.is_empty() && port.len() <= 5 && port.parse::<u16>().is_ok()
}

fn is_ipv4(host: &str) -> bool {
    let parts = host.split('.').collect::<Vec<_>>();
    parts.len() == 4
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.parse::<u8>().is_ok())
}

fn is_domain_name(host: &str) -> bool {
    if !host.contains('.') {
        return false;
    }

    let mut labels = host.split('.');
    let Some(tld) = labels.next_back() else {
        return false;
    };
    is_tld(tld) && labels.all(is_domain_label)
}

fn is_tld(label: &str) -> bool {
    (2..=63).contains(&label.len()) && label.chars().all(|ch| ch.is_ascii_alphabetic())
}

fn is_domain_label(label: &str) -> bool {
    if label.is_empty() || label.len() > 63 {
        return false;
    }

    let Some(first) = label.chars().next() else {
        return false;
    };
    let Some(last) = label.chars().next_back() else {
        return false;
    };
    first.is_ascii_alphanumeric()
        && last.is_ascii_alphanumeric()
        && label
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
}
