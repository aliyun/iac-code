use iac_code_tui::{
    format_resume_session_size, short_resume_session_id, ResumePickerState, ResumeSessionEntry,
    WHEEL_LINES,
};

#[test]
fn resume_picker_initializes_current_project_and_excludes_current_session() {
    let picker = ResumePickerState::new(
        vec![entry("id-aa", "alpha"), entry("id-ab", "beta")],
        vec![
            entry("id-aa", "alpha"),
            entry("id-ab", "beta"),
            entry("id-bc", "other project"),
        ],
        Some("id-ab"),
        Some("main"),
        10,
    );

    assert_eq!(ids(picker.filtered_entries()), vec!["id-aa"]);
    assert_eq!(ids(picker.visible_entries()), vec!["id-aa"]);
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);
    assert!(!picker.show_all_projects());
    assert!(!picker.only_current_branch());
}

#[test]
fn resume_picker_toggles_all_projects_and_current_branch_filter() {
    let mut picker = ResumePickerState::new(
        vec![
            entry("id-aa", "alpha").with_git_branch("main"),
            entry("id-ab", "beta").with_git_branch("dev"),
        ],
        vec![
            entry("id-aa", "alpha").with_git_branch("main"),
            entry("id-ab", "beta").with_git_branch("dev"),
            entry("id-bc", "other project")
                .with_project_name("b")
                .with_git_branch("main"),
        ],
        None,
        Some("main"),
        10,
    );

    assert_eq!(ids(picker.filtered_entries()), vec!["id-aa", "id-ab"]);

    picker.toggle_show_all_projects();
    assert!(picker.show_all_projects());
    assert_eq!(
        ids(picker.filtered_entries()),
        vec!["id-aa", "id-ab", "id-bc"]
    );

    picker.toggle_only_current_branch();
    assert!(picker.only_current_branch());
    assert_eq!(ids(picker.filtered_entries()), vec!["id-aa", "id-bc"]);
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);
}

#[test]
fn resume_picker_search_matches_python_haystack_and_id_prefix_wins() {
    let mut picker = ResumePickerState::new(
        vec![
            entry("abc-123", "unrelated")
                .with_project_name("networking")
                .with_git_branch("feature-branch")
                .with_name("named-session")
                .with_auto_title("auto title text"),
            entry("session-xyz", "abc title"),
        ],
        vec![],
        None,
        None,
        10,
    );

    for query in ["abc", "named", "auto", "networking", "feature"] {
        picker.update_query(query);
        assert_eq!(ids(picker.filtered_entries())[0], "abc-123");
        assert_eq!(picker.focused_index(), 0);
        assert_eq!(picker.visible_from(), 0);
    }

    picker.update_query("/");
    assert!(picker.filtered_entries().is_empty());
}

#[test]
fn resume_picker_moves_focus_pages_and_updates_visible_window() {
    let mut picker = ResumePickerState::new(
        vec![
            entry("a", "a"),
            entry("b", "b"),
            entry("c", "c"),
            entry("d", "d"),
        ],
        vec![],
        None,
        None,
        2,
    );

    picker.move_focus(3);
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
    assert_eq!(ids(picker.visible_entries()), vec!["c", "d"]);

    picker.page_up();
    assert_eq!(picker.focused_index(), 1);
    assert_eq!(picker.visible_from(), 1);

    picker.page_down();
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
}

#[test]
fn resume_picker_preview_mode_scrolls_without_changing_list_focus() {
    let mut picker = ResumePickerState::new(
        vec![entry("a", "a"), entry("b", "b")],
        vec![],
        None,
        None,
        10,
    );

    picker.update_query("a");
    assert!(!picker.enter_preview());
    assert!(!picker.is_previewing());

    picker.update_query("");
    picker.move_focus(1);
    picker.set_preview_body_height(10);
    assert!(picker.enter_preview());
    assert!(picker.is_previewing());
    assert_eq!(picker.preview_scroll_offset(), 0);

    picker.scroll_preview(1);
    assert_eq!(picker.preview_scroll_offset(), 1);
    picker.scroll_preview(-1);
    assert_eq!(picker.preview_scroll_offset(), 0);
    picker.scroll_preview(-1);
    assert_eq!(picker.preview_scroll_offset(), 0);

    picker.wheel_preview_up();
    assert_eq!(picker.preview_scroll_offset(), WHEEL_LINES);
    picker.wheel_preview_down();
    assert_eq!(picker.preview_scroll_offset(), 0);

    picker.page_preview_up();
    assert_eq!(picker.preview_scroll_offset(), 9);
    picker.page_preview_down();
    assert_eq!(picker.preview_scroll_offset(), 0);

    picker.jump_preview_start();
    assert!(picker.preview_scroll_offset() >= 1 << 20);
    picker.jump_preview_end();
    assert_eq!(picker.preview_scroll_offset(), 0);
    assert_eq!(picker.focused_index(), 1);

    picker.exit_preview();
    assert!(!picker.is_previewing());
    assert!(!picker.is_done());
}

#[test]
fn resume_picker_select_cancel_and_format_helpers_match_python() {
    let mut picker = ResumePickerState::new(
        vec![entry("1234567890abcdef", "deploy-prod").with_name("deploy-prod")],
        vec![],
        None,
        None,
        10,
    );

    let selected = picker
        .select_focused()
        .expect("focused session should select");
    assert!(picker.is_done());
    assert_eq!(selected.session_id, "1234567890abcdef");
    assert_eq!(
        picker.result().map(|entry| entry.session_id.as_str()),
        Some("1234567890abcdef")
    );

    let mut cancelled =
        ResumePickerState::new(vec![entry("id-aa", "alpha")], vec![], None, None, 10);
    cancelled.cancel();
    assert!(cancelled.is_done());
    assert!(cancelled.result().is_none());

    assert_eq!(short_resume_session_id("1234567890abcdef"), "12345678");
    assert_eq!(short_resume_session_id("short"), "short");
    assert_eq!(format_resume_session_size(500), "500B");
    assert_eq!(format_resume_session_size(2048), "2.0KB");
    assert_eq!(format_resume_session_size(2 * 1024 * 1024), "2.0MB");
}

fn entry(session_id: &str, title: &str) -> ResumeSessionEntry {
    ResumeSessionEntry::new(session_id, "/proj/a", "a", title, 1_700_000_000, 42)
        .with_git_branch("main")
}

fn ids(entries: &[ResumeSessionEntry]) -> Vec<&str> {
    entries
        .iter()
        .map(|entry| entry.session_id.as_str())
        .collect()
}
