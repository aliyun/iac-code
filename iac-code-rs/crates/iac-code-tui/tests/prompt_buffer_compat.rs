use iac_code_tui::PromptBuffer;

#[test]
fn prompt_buffer_matches_python_search_box_basic_editing() {
    let mut buffer = PromptBuffer::from_text("hllo");

    buffer.move_home();
    buffer.move_right();
    buffer.insert_text("e");

    assert_eq!(buffer.text(), "hello");
    assert_eq!(buffer.cursor(), 2);

    buffer.backspace();
    assert_eq!(buffer.text(), "hllo");
    assert_eq!(buffer.cursor(), 1);

    buffer.delete();
    assert_eq!(buffer.text(), "hlo");
    assert_eq!(buffer.cursor(), 1);
}

#[test]
fn prompt_buffer_matches_python_search_box_control_deletions() {
    let mut buffer = PromptBuffer::from_text("hello world");
    buffer.move_home();
    for _ in 0..5 {
        buffer.move_right();
    }

    buffer.kill_to_end();
    assert_eq!(buffer.text(), "hello");
    assert_eq!(buffer.cursor(), 5);

    buffer.insert_text("   world");
    buffer.delete_previous_word();
    assert_eq!(buffer.text(), "hello   ");
    assert_eq!(buffer.cursor(), 8);

    buffer.delete_previous_word();
    assert_eq!(buffer.text(), "hello");
    assert_eq!(buffer.cursor(), 5);

    buffer.kill_to_start();
    assert_eq!(buffer.text(), "");
    assert_eq!(buffer.cursor(), 0);
}

#[test]
fn prompt_buffer_preserves_utf8_boundaries_for_cursor_and_deletion() {
    let mut buffer = PromptBuffer::from_text("阿里云");

    buffer.move_left();
    assert_eq!(buffer.cursor(), "阿里".len());

    buffer.insert_text(" Rust ");
    assert_eq!(buffer.text(), "阿里 Rust 云");
    assert_eq!(buffer.cursor(), "阿里 Rust ".len());

    buffer.backspace();
    assert_eq!(buffer.text(), "阿里 Rust云");
    assert_eq!(buffer.cursor(), "阿里 Rust".len());

    buffer.move_left();
    buffer.delete();
    assert_eq!(buffer.text(), "阿里 Rus云");
    assert_eq!(buffer.cursor(), "阿里 Rus".len());
}

#[test]
fn prompt_buffer_moves_and_deletes_by_grapheme_cluster() {
    let mut buffer = PromptBuffer::from_text("a🇨🇳e\u{301}b");

    buffer.move_left();
    assert_eq!(buffer.cursor(), "a🇨🇳e\u{301}".len());

    buffer.move_left();
    assert_eq!(buffer.cursor(), "a🇨🇳".len());

    buffer.backspace();
    assert_eq!(buffer.text(), "ae\u{301}b");
    assert_eq!(buffer.cursor(), "a".len());

    buffer.move_right();
    assert_eq!(buffer.cursor(), "ae\u{301}".len());

    buffer.move_home();
    buffer.move_right();
    buffer.delete();
    assert_eq!(buffer.text(), "ab");
    assert_eq!(buffer.cursor(), "a".len());
}

#[test]
fn prompt_buffer_replaces_suggestion_token_ranges() {
    let mut buffer = PromptBuffer::from_text("run /mod now");

    buffer.set_cursor(8);
    buffer.replace_range(4..8, "/model ");

    assert_eq!(buffer.text(), "run /model  now");
    assert_eq!(buffer.cursor(), 11);
}

#[test]
fn prompt_buffer_deletes_image_placeholders_atomically() {
    let mut buffer = PromptBuffer::from_text("[Image #1] describe");

    buffer.set_cursor("[Image #1]".len());
    buffer.backspace();
    assert_eq!(buffer.text(), " describe");
    assert_eq!(buffer.cursor(), 0);

    let mut buffer = PromptBuffer::from_text("[Image #12] describe");
    buffer.set_cursor("[Image".len());
    buffer.backspace();
    assert_eq!(buffer.text(), " describe");
    assert_eq!(buffer.cursor(), 0);

    let mut buffer = PromptBuffer::from_text("[Image #2] describe");
    buffer.set_cursor(0);
    buffer.delete();
    assert_eq!(buffer.text(), " describe");
    assert_eq!(buffer.cursor(), 0);
}
