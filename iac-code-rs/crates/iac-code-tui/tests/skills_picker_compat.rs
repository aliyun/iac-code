use std::collections::BTreeSet;

use iac_code_tui::{SkillManagementItem, SkillManagementSource, SkillsPickerState, SkillsSortMode};

#[test]
fn skills_picker_initializes_disabled_set_and_sorts_by_name_like_python() {
    let picker = SkillsPickerState::new(
        vec![
            item(
                "zeta",
                SkillManagementSource::User,
                true,
                false,
                400,
                "zeta description",
            ),
            item(
                "bundled",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "bundled description",
            ),
            item(
                "Team Review",
                SkillManagementSource::Project,
                false,
                false,
                800,
                "review",
            ),
            item(
                "$locked-off",
                SkillManagementSource::Bundled,
                false,
                true,
                200,
                "locked stays enabled",
            ),
        ],
        10,
    );

    assert_eq!(
        names(picker.filtered_items()),
        vec!["$locked-off", "Team Review", "bundled", "zeta"]
    );
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(
        picker.disabled_skill_names(),
        BTreeSet::from(["team review".to_owned()])
    );
}

#[test]
fn skills_picker_filters_by_name_and_description_and_tracks_description_matches() {
    let mut picker = SkillsPickerState::new(
        vec![
            item(
                "iac-aliyun",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "Terraform template",
            ),
            item(
                "team-review",
                SkillManagementSource::Project,
                true,
                false,
                100,
                "Terraform template",
            ),
            item(
                "deploy",
                SkillManagementSource::User,
                true,
                false,
                100,
                "deploy",
            ),
        ],
        10,
    );

    picker.update_query("t");

    assert_eq!(
        names(picker.filtered_items()),
        vec!["iac-aliyun", "team-review"]
    );
    assert!(picker.description_matched_names().contains("iac-aliyun"));
    assert!(!picker.description_matched_names().contains("team-review"));

    picker.update_query("/");
    assert!(picker.filtered_items().is_empty());
}

#[test]
fn skills_picker_does_not_match_description_by_sparse_subsequence() {
    let mut picker = SkillsPickerState::new(
        vec![
            item(
                "iac-aliyun",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "Alibaba Cloud template generation",
            ),
            item(
                "simplify",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "Review changed code for reuse, quality, and efficiency.",
            ),
        ],
        10,
    );

    picker.update_query("iac");
    assert_eq!(names(picker.filtered_items()), vec!["iac-aliyun"]);

    picker.update_query("quality");
    assert_eq!(names(picker.filtered_items()), vec!["simplify"]);
    assert!(picker.description_matched_names().contains("simplify"));
}

#[test]
fn skills_picker_cycles_sort_modes_and_preserves_focused_skill() {
    let mut picker = SkillsPickerState::new(
        vec![
            item(
                "zeta",
                SkillManagementSource::User,
                true,
                false,
                400,
                "zeta",
            ),
            item(
                "alpha",
                SkillManagementSource::Project,
                true,
                false,
                800,
                "alpha",
            ),
            item(
                "bundled",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "bundled",
            ),
        ],
        10,
    );

    assert_eq!(
        names(picker.filtered_items()),
        vec!["alpha", "bundled", "zeta"]
    );
    picker.move_focus(2);
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );

    picker.cycle_sort();
    assert_eq!(picker.sort_mode(), SkillsSortMode::Source);
    assert_eq!(
        names(picker.filtered_items()),
        vec!["bundled", "alpha", "zeta"]
    );
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );

    picker.cycle_sort();
    assert_eq!(picker.sort_mode(), SkillsSortMode::Size);
    assert_eq!(
        names(picker.filtered_items()),
        vec!["bundled", "zeta", "alpha"]
    );
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );
}

#[test]
fn skills_picker_moves_focus_pages_and_updates_visible_window() {
    let mut picker = SkillsPickerState::new(
        vec![
            item("a", SkillManagementSource::Project, true, false, 100, "a"),
            item("b", SkillManagementSource::Project, true, false, 100, "b"),
            item("c", SkillManagementSource::Project, true, false, 100, "c"),
            item("d", SkillManagementSource::Project, true, false, 100, "d"),
        ],
        2,
    );

    picker.move_focus(3);
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
    assert_eq!(names(picker.visible_items()), vec!["c", "d"]);

    picker.page_up();
    assert_eq!(picker.focused_index(), 1);
    assert_eq!(picker.visible_from(), 1);

    picker.page_down();
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
}

#[test]
fn skills_picker_update_query_resets_window_and_clears_status() {
    let mut picker = SkillsPickerState::new(
        vec![
            item("a", SkillManagementSource::Bundled, true, true, 100, "a"),
            item("b", SkillManagementSource::Project, true, false, 100, "b"),
            item("c", SkillManagementSource::Project, true, false, 100, "c"),
            item("d", SkillManagementSource::Project, true, false, 100, "d"),
        ],
        2,
    );

    picker.toggle_focused();
    assert!(picker.status_message().contains("cannot be disabled"));
    picker.move_focus(3);
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);

    picker.update_query("b");

    assert_eq!(names(picker.filtered_items()), vec!["b"]);
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);
    assert_eq!(picker.status_message(), "");
}

#[test]
fn skills_picker_sort_and_unlocked_toggle_preserve_focus_but_reset_window() {
    let mut picker = SkillsPickerState::new(
        vec![
            item(
                "zeta",
                SkillManagementSource::User,
                true,
                false,
                400,
                "zeta",
            ),
            item(
                "alpha",
                SkillManagementSource::Project,
                true,
                false,
                800,
                "alpha",
            ),
            item(
                "bundled",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "bundled",
            ),
            item(
                "beta",
                SkillManagementSource::Project,
                true,
                false,
                200,
                "beta",
            ),
        ],
        2,
    );

    picker.move_focus(3);
    assert_eq!(picker.visible_from(), 2);
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );

    picker.cycle_sort();
    assert_eq!(picker.sort_mode(), SkillsSortMode::Source);
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );
    assert_eq!(picker.visible_from(), 0);

    picker.toggle_focused();
    assert_eq!(
        picker.focused_item().map(|item| item.name.as_str()),
        Some("zeta")
    );
    assert_eq!(picker.visible_from(), 0);
    assert_eq!(
        picker.disabled_skill_names(),
        BTreeSet::from(["zeta".to_owned()])
    );
}

#[test]
fn skills_picker_locked_toggle_keeps_current_window() {
    let mut picker = SkillsPickerState::new(
        vec![
            item("a", SkillManagementSource::Project, true, false, 100, "a"),
            item("b", SkillManagementSource::Project, true, false, 100, "b"),
            item("c", SkillManagementSource::Project, true, false, 100, "c"),
            item("d", SkillManagementSource::Bundled, true, true, 100, "d"),
        ],
        2,
    );

    picker.move_focus(3);
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);

    picker.toggle_focused();

    assert!(picker.status_message().contains("cannot be disabled"));
    assert_eq!(picker.focused_index(), 3);
    assert_eq!(picker.visible_from(), 2);
}

#[test]
fn skills_picker_empty_results_navigation_and_toggle_are_noops() {
    let mut picker = SkillsPickerState::new(
        vec![item(
            "a",
            SkillManagementSource::Project,
            true,
            false,
            100,
            "a",
        )],
        0,
    );

    picker.update_query("/");
    picker.move_focus(1);
    picker.page_down();
    picker.page_up();
    picker.toggle_focused();

    assert!(picker.filtered_items().is_empty());
    assert!(picker.visible_items().is_empty());
    assert_eq!(picker.focused_index(), 0);
    assert_eq!(picker.visible_from(), 0);
    assert_eq!(picker.disabled_skill_names(), BTreeSet::new());
    assert_eq!(picker.status_message(), "");
}

#[test]
fn skills_picker_toggles_unlocked_skills_rejects_locked_and_saves_or_cancels() {
    let mut picker = SkillsPickerState::new(
        vec![
            item(
                "iac-aliyun",
                SkillManagementSource::Bundled,
                true,
                true,
                100,
                "core",
            ),
            item(
                "team-review",
                SkillManagementSource::Project,
                true,
                false,
                100,
                "review",
            ),
        ],
        10,
    );

    picker.toggle_focused();
    assert_eq!(picker.disabled_skill_names(), BTreeSet::new());
    assert!(picker.status_message().contains("cannot be disabled"));

    picker.move_focus(1);
    picker.toggle_focused();
    assert_eq!(
        picker.disabled_skill_names(),
        BTreeSet::from(["team-review".to_owned()])
    );
    assert_eq!(picker.status_message(), "");

    let saved = picker.save();
    assert_eq!(saved, BTreeSet::from(["team-review".to_owned()]));
    assert!(picker.is_done());
    assert_eq!(
        picker.result(),
        Some(&BTreeSet::from(["team-review".to_owned()]))
    );

    let mut cancelled = SkillsPickerState::new(
        vec![item(
            "team-review",
            SkillManagementSource::Project,
            true,
            false,
            100,
            "review",
        )],
        10,
    );
    cancelled.cancel();
    assert!(cancelled.is_done());
    assert!(cancelled.result().is_none());
}

fn item(
    name: &str,
    source: SkillManagementSource,
    enabled: bool,
    locked: bool,
    size: usize,
    description: &str,
) -> SkillManagementItem {
    SkillManagementItem::new(
        name,
        description,
        source,
        size,
        format!("/repo/{name}"),
        enabled,
        locked,
    )
}

fn names(items: &[SkillManagementItem]) -> Vec<&str> {
    items.iter().map(|item| item.name.as_str()).collect()
}
