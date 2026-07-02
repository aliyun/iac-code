use iac_code_tui::{
    decode_terminal_input, is_bracketed_paste_start, parse_bracketed_paste_bytes,
    parse_terminal_escape_sequence, parse_terminal_key_byte,
};

#[test]
fn terminal_key_byte_parser_matches_python_raw_input_basics() {
    assert_key(parse_terminal_key_byte(13), "enter", false, false, false);
    assert_key(parse_terminal_key_byte(10), "enter", false, false, false);
    assert_key(parse_terminal_key_byte(9), "tab", false, false, false);
    assert_key(
        parse_terminal_key_byte(127),
        "backspace",
        false,
        false,
        false,
    );
    assert_key(parse_terminal_key_byte(27), "escape", false, false, false);

    let ctrl_a = parse_terminal_key_byte(1);
    assert_key(ctrl_a.clone(), "a", true, false, false);
    assert_eq!(ctrl_a.key_id(), "ctrl+a");

    let ctrl_z = parse_terminal_key_byte(26);
    assert_key(ctrl_z.clone(), "z", true, false, false);
    assert_eq!(ctrl_z.key_id(), "ctrl+z");

    let lower = parse_terminal_key_byte(b'a');
    assert_key(lower.clone(), "a", false, false, false);
    assert_eq!(lower.char_text, "a");

    let upper = parse_terminal_key_byte(b'A');
    assert_key(upper.clone(), "A", false, false, true);
    assert_eq!(upper.char_text, "A");
}

#[test]
fn terminal_escape_parser_matches_python_named_sequences_and_alt_chars() {
    for (sequence, key) in [
        ("[A", "up"),
        ("[B", "down"),
        ("[C", "right"),
        ("[D", "left"),
        ("[H", "home"),
        ("[F", "end"),
        ("[3~", "delete"),
        ("[5~", "pageup"),
        ("[6~", "pagedown"),
        ("[I", "focus_in"),
        ("[O", "focus_out"),
        ("OP", "f1"),
        ("OQ", "f2"),
        ("OR", "f3"),
        ("OS", "f4"),
    ] {
        assert_key(
            parse_terminal_escape_sequence(sequence),
            key,
            false,
            false,
            false,
        );
    }

    let alt_p = parse_terminal_escape_sequence("p");
    assert_key(alt_p.clone(), "p", false, true, false);
    assert_eq!(alt_p.key_id(), "alt+p");

    assert_key(
        parse_terminal_escape_sequence("[999~"),
        "unknown",
        false,
        false,
        false,
    );
}

#[test]
fn terminal_escape_parser_matches_python_modified_key_sequences() {
    assert_key(
        parse_terminal_escape_sequence("[13;2u"),
        "enter",
        false,
        false,
        true,
    );
    assert_key(
        parse_terminal_escape_sequence("[27;2;13~"),
        "enter",
        false,
        false,
        true,
    );
    assert_key(
        parse_terminal_escape_sequence("[13;2~"),
        "enter",
        false,
        false,
        true,
    );
    assert_key(
        parse_terminal_escape_sequence("[13;5u"),
        "unknown",
        false,
        false,
        false,
    );

    let ctrl_c = parse_terminal_escape_sequence("[27;5;99~");
    assert_key(ctrl_c.clone(), "c", true, false, false);
    assert_eq!(ctrl_c.key_id(), "ctrl+c");

    let ctrl_r = parse_terminal_escape_sequence("[114;5u");
    assert_key(ctrl_r.clone(), "r", true, false, false);
    assert_eq!(ctrl_r.key_id(), "ctrl+r");
}

#[test]
fn terminal_escape_parser_matches_python_mouse_sgr_shortcuts() {
    assert_key(
        parse_terminal_escape_sequence("[<64;10;5M"),
        "wheel_up",
        false,
        false,
        false,
    );
    assert_key(
        parse_terminal_escape_sequence("[<65;10;5M"),
        "wheel_down",
        false,
        false,
        false,
    );
    assert_key(
        parse_terminal_escape_sequence("[<0;3;7M"),
        "mouse",
        false,
        false,
        false,
    );
    assert_key(
        parse_terminal_escape_sequence("[<64;10;5M[<64;10;5M"),
        "wheel_up",
        false,
        false,
        false,
    );
}

#[test]
fn bracketed_paste_parser_matches_python_marker_stripping_and_newline_normalization() {
    assert!(is_bracketed_paste_start(b"[200~hello"));
    assert!(!is_bracketed_paste_start(b"[201~hello"));

    let event = parse_bracketed_paste_bytes(b"hello\r\nworld\x1b[201~ignored");
    assert_key(event.clone(), "paste", false, false, false);
    assert_eq!(event.char_text, "hello\nworld");

    let timeout_event = parse_bracketed_paste_bytes(b"hello\rworld");
    assert_key(timeout_event.clone(), "paste", false, false, false);
    assert_eq!(timeout_event.char_text, "hello\nworld");

    let lossy_event = parse_bracketed_paste_bytes(b"bad utf8: \xff\x1b[201~");
    assert_eq!(lossy_event.char_text, "bad utf8: \u{fffd}");
}

#[test]
fn terminal_input_decoder_routes_complete_raw_sequences_like_python_read_key() {
    assert!(decode_terminal_input(b"").is_none());

    assert_key(
        decode_terminal_input(b"\x1b").expect("escape event"),
        "escape",
        false,
        false,
        false,
    );
    assert_key(
        decode_terminal_input(b"\x1b[A").expect("arrow event"),
        "up",
        false,
        false,
        false,
    );

    let paste = decode_terminal_input(b"\x1b[200~hello\r\nworld\x1b[201~").expect("paste event");
    assert_key(paste.clone(), "paste", false, false, false);
    assert_eq!(paste.char_text, "hello\nworld");

    let utf8 = decode_terminal_input("你".as_bytes()).expect("utf8 event");
    assert_key(utf8.clone(), "你", false, false, false);
    assert_eq!(utf8.char_text, "你");

    assert_key(
        decode_terminal_input(b"\xe4\xff").expect("invalid utf8 event"),
        "unknown",
        false,
        false,
        false,
    );
}

fn assert_key(event: iac_code_tui::PromptKeyEvent, key: &str, ctrl: bool, alt: bool, shift: bool) {
    assert_eq!(event.key, key);
    assert_eq!(event.ctrl, ctrl);
    assert_eq!(event.alt, alt);
    assert_eq!(event.shift, shift);
}
