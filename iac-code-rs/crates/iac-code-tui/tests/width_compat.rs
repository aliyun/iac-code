use iac_code_tui::{
    suffix_start_for_display_width, take_prefix_by_display_width, terminal_display_width,
    truncate_to_display_width, usable_content_width,
};

#[test]
fn tui_width_guard_uses_strict_positive_content_width() {
    assert_eq!(usable_content_width(0, 0), None);
    assert_eq!(usable_content_width(2, 2), None);
    assert_eq!(usable_content_width(3, 4), None);
    assert_eq!(usable_content_width(5, 4), Some(1));
}

#[test]
fn tui_display_width_counts_wide_and_combined_graphemes() {
    assert_eq!(terminal_display_width("abc"), 3);
    assert_eq!(terminal_display_width("阿里"), 4);
    assert_eq!(terminal_display_width("e\u{301}"), 1);
    assert_eq!(terminal_display_width("👨‍👩‍👧‍👦"), 2);
}

#[test]
fn tui_truncates_to_display_width_without_splitting_graphemes() {
    assert_eq!(truncate_to_display_width("阿里abc", 3), "阿");
    assert_eq!(truncate_to_display_width("阿里abc", 4), "阿里");
    assert_eq!(truncate_to_display_width("a👨‍👩‍👧‍👦b", 3), "a👨‍👩‍👧‍👦");
}

#[test]
fn tui_takes_prefix_by_display_width_without_splitting_graphemes() {
    assert_eq!(take_prefix_by_display_width("阿里abc", 5), ("阿里a", "bc"));
    assert_eq!(take_prefix_by_display_width("a👨‍👩‍👧‍👦b", 2), ("a", "👨‍👩‍👧‍👦b"));
    assert_eq!(take_prefix_by_display_width("a👨‍👩‍👧‍👦b", 3), ("a👨‍👩‍👧‍👦", "b"));
}

#[test]
fn tui_finds_suffix_start_by_display_width_without_splitting_graphemes() {
    assert_eq!(suffix_start_for_display_width("模型abc", 3), "模型".len());
    assert_eq!(suffix_start_for_display_width("模型abc", 4), "模型".len());
    assert_eq!(suffix_start_for_display_width("模型abc", 5), "模".len());
    assert_eq!(suffix_start_for_display_width("a👨‍👩‍👧‍👦b", 3), "a".len());
}
