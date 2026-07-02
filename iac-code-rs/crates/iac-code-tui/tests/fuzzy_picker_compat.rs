use iac_code_tui::{fuzzy_match, FuzzyPickerState, PickerItem};

#[test]
fn fuzzy_picker_static_items_filter_sort_and_count_like_python() {
    let mut picker = FuzzyPickerState::new_static(make_items(), 10);

    assert_eq!(
        displays(picker.filtered_items()),
        vec!["Alpha", "Beta", "Gamma", "Delta"]
    );
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.match_count_text(), "4/4 matches");

    picker.update_query("al");

    assert_eq!(displays(picker.filtered_items()), vec!["Alpha"]);
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);
    assert_eq!(picker.match_count_text(), "1/4 matches");
}

#[test]
fn fuzzy_picker_focus_moves_and_scrolls_with_python_clamping() {
    let mut picker = FuzzyPickerState::new_static(make_items(), 2);

    picker.move_focus(3);
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
    assert_eq!(displays(picker.visible_items()), vec!["Gamma", "Delta"]);

    picker.move_focus(-2);
    assert_eq!(picker.focused_index(), 1);
    assert_eq!(picker.visible_from(), 1);
    assert_eq!(displays(picker.visible_items()), vec!["Beta", "Gamma"]);

    picker.page_up();
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);

    picker.page_down();
    assert_eq!(picker.focused_index(), 2);
    assert_eq!(picker.visible_from(), 1);
}

#[test]
fn fuzzy_picker_selection_and_empty_results_match_python_state() {
    let mut picker = FuzzyPickerState::new_static(make_items(), 10);

    picker.move_focus(1);
    let selected = picker.select_focused().expect("focused item should select");
    assert_eq!(selected.key, "b");
    assert!(picker.is_done());
    assert_eq!(
        picker.result().map(|item| item.display.as_str()),
        Some("Beta")
    );

    picker.update_query("zzz");
    assert!(picker.filtered_items().is_empty());
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.select_focused(), None);
    assert!(
        picker.is_done(),
        "empty enter should not clear prior done state"
    );
}

#[test]
fn fuzzy_picker_dynamic_items_call_factory_and_use_results_count_text() {
    let mut picker = FuzzyPickerState::new_dynamic(
        |query| vec![PickerItem::new(query, format!("Result: {query}"))],
        10,
    );

    assert_eq!(displays(picker.filtered_items()), vec!["Result: "]);
    assert_eq!(picker.match_count_text(), "1 results");

    picker.update_query("hello");
    assert_eq!(displays(picker.filtered_items()), vec!["Result: hello"]);
    assert_eq!(picker.match_count_text(), "1 results");
}

#[test]
fn picker_item_defaults_filter_text_to_display_and_allows_override() {
    let default_item = PickerItem::new("id", "Display Name");
    assert_eq!(default_item.filter_text, "Display Name");

    let overridden = PickerItem::new("id", "Display Name").with_filter_text("metadata alias");
    assert_eq!(overridden.filter_text, "metadata alias");
    assert!(fuzzy_match("ma", &overridden.filter_text).is_some());
}

fn make_items() -> Vec<PickerItem> {
    vec![
        PickerItem::new("a", "Alpha"),
        PickerItem::new("b", "Beta"),
        PickerItem::new("c", "Gamma"),
        PickerItem::new("d", "Delta"),
    ]
}

fn displays(items: &[PickerItem]) -> Vec<&str> {
    items.iter().map(|item| item.display.as_str()).collect()
}
