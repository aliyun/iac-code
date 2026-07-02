use iac_code_tui::{
    HistoryContentBlock, HistoryMessage, HistoryMessageContent, HistorySearchItem,
    HistorySearchState,
};

#[test]
fn history_search_builds_user_items_most_recent_first_like_python() {
    let state = HistorySearchState::new(
        vec![
            HistoryMessage::text("user", "First user message"),
            HistoryMessage::text("assistant", "Assistant reply"),
            HistoryMessage::text("user", "Second user message"),
        ],
        10,
    );

    assert_eq!(
        displays(state.filtered_items()),
        vec!["Second user message", "First user message"]
    );
    assert_eq!(
        contents(state.filtered_items()),
        vec!["Second user message", "First user message"]
    );
    assert_eq!(keys(state.filtered_items()), vec!["history-0", "history-1"]);
}

#[test]
fn history_search_display_truncates_to_80_chars_but_filter_uses_full_content() {
    let long_content = "A".repeat(200);
    let state = HistorySearchState::new(vec![HistoryMessage::text("user", &long_content)], 10);
    let item = &state.filtered_items()[0];

    assert_eq!(item.display, "A".repeat(80));
    assert_eq!(item.filter_text, long_content);
    assert_eq!(item.content, long_content);
}

#[test]
fn history_search_handles_structured_content_blocks_like_python() {
    let state = HistorySearchState::new(
        vec![
            HistoryMessage::new(
                "user",
                HistoryMessageContent::Blocks(vec![
                    HistoryContentBlock::text("Hello"),
                    HistoryContentBlock::text("World"),
                ]),
            ),
            HistoryMessage::new(
                "user",
                HistoryMessageContent::Blocks(vec![
                    HistoryContentBlock::other("plain string"),
                    HistoryContentBlock::text("structured"),
                ]),
            ),
            HistoryMessage::new(
                "user",
                HistoryMessageContent::Blocks(vec![
                    HistoryContentBlock::empty(),
                    HistoryContentBlock::text("caption"),
                ]),
            ),
        ],
        10,
    );

    assert_eq!(
        contents(state.filtered_items()),
        vec![" caption", "plain string structured", "Hello World"]
    );
}

#[test]
fn history_search_filters_focuses_pages_and_updates_visible_window() {
    let mut state = HistorySearchState::new(
        vec![
            HistoryMessage::text("user", "alpha network"),
            HistoryMessage::text("user", "beta compute"),
            HistoryMessage::text("user", "gamma storage"),
            HistoryMessage::text("assistant", "network assistant"),
        ],
        2,
    );

    assert_eq!(
        contents(state.filtered_items()),
        vec!["gamma storage", "beta compute", "alpha network"]
    );

    state.update_query("net");
    assert_eq!(contents(state.filtered_items()), vec!["alpha network"]);
    assert_eq!(state.focused_index(), 0);
    assert_eq!(state.visible_from(), 0);

    state.update_query("");
    state.move_focus(2);
    assert_eq!(state.focused_index(), 2);
    assert_eq!(state.visible_from(), 1);
    assert_eq!(
        contents(state.visible_items()),
        vec!["beta compute", "alpha network"]
    );

    state.page_up();
    assert_eq!(state.focused_index(), 0);
    assert_eq!(state.visible_from(), 0);

    state.page_down();
    assert_eq!(state.focused_index(), 2);
    assert_eq!(state.visible_from(), 1);
}

#[test]
fn history_search_select_and_cancel_match_python_result_state() {
    let mut state =
        HistorySearchState::new(vec![HistoryMessage::text("user", "hello from history")], 10);

    let selected = state.select_focused().expect("focused item should select");
    assert_eq!(selected.content, "hello from history");
    assert!(state.is_done());
    assert_eq!(
        state.result().map(String::as_str),
        Some("hello from history")
    );

    let mut cancelled =
        HistorySearchState::new(vec![HistoryMessage::text("user", "some message")], 10);
    cancelled.cancel();
    assert!(cancelled.is_done());
    assert!(cancelled.result().is_none());
}

fn displays(items: &[HistorySearchItem]) -> Vec<&str> {
    items.iter().map(|item| item.display.as_str()).collect()
}

fn contents(items: &[HistorySearchItem]) -> Vec<&str> {
    items.iter().map(|item| item.content.as_str()).collect()
}

fn keys(items: &[HistorySearchItem]) -> Vec<&str> {
    items.iter().map(|item| item.key.as_str()).collect()
}
