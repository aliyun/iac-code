use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_tui::{
    CompletionToken, InputHistory, KeyBinding, KeybindingManager, PromptEditOutcome, PromptEditor,
    PromptKeyEvent, SuggestionItem, SuggestionProvider,
};

#[test]
fn prompt_editor_handles_python_prompt_input_editing_keys() {
    let mut editor = PromptEditor::new(vec![]);

    type_text(&mut editor, "deploy stack");
    assert_eq!(editor.text(), "deploy stack");
    assert_eq!(editor.cursor(), "deploy stack".len());

    assert_eq!(editor.handle_key(ctrl("a")), PromptEditOutcome::Continue);
    type_text(&mut editor, "ros ");
    assert_eq!(editor.text(), "ros deploy stack");
    assert_eq!(editor.cursor(), "ros ".len());

    assert_eq!(editor.handle_key(ctrl("e")), PromptEditOutcome::Continue);
    assert_eq!(editor.handle_key(ctrl("w")), PromptEditOutcome::Continue);
    assert_eq!(editor.text(), "ros deploy ");
    assert_eq!(editor.cursor(), "ros deploy ".len());

    type_text(&mut editor, "template");
    assert_eq!(editor.handle_key(key("left")), PromptEditOutcome::Continue);
    assert_eq!(editor.handle_key(key("left")), PromptEditOutcome::Continue);
    assert_eq!(
        editor.handle_key(key("delete")),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.text(), "ros deploy templae");

    assert_eq!(editor.handle_key(ctrl("k")), PromptEditOutcome::Continue);
    assert_eq!(editor.text(), "ros deploy templa");
    assert_eq!(editor.handle_key(ctrl("u")), PromptEditOutcome::Continue);
    assert_eq!(editor.text(), "");

    type_text(&mut editor, "final");
    assert_eq!(
        editor.handle_key(key("enter")),
        PromptEditOutcome::Submit("final".to_owned())
    );
}

#[test]
fn prompt_editor_handles_newline_and_cancel_keys_like_python_prompt_input() {
    let mut editor = PromptEditor::new(vec![]);

    type_text(&mut editor, "line1");
    assert_eq!(
        editor.handle_key(shift("enter")),
        PromptEditOutcome::Continue
    );
    type_text(&mut editor, "line2");
    assert_eq!(
        editor.handle_key(key("escape")),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.handle_key(key("enter")), PromptEditOutcome::Continue);
    type_text(&mut editor, "line3");

    assert_eq!(editor.text(), "line1\nline2\nline3");
    assert_eq!(editor.handle_key(ctrl("c")), PromptEditOutcome::Continue);
    assert_eq!(editor.text(), "");
    assert_eq!(editor.handle_key(ctrl("c")), PromptEditOutcome::Cancel);
}

#[test]
fn prompt_editor_accepts_suggestions_through_keyboard_events() {
    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("model", "/model ", 10.0)],
    ))]);

    type_text(&mut editor, "/mod");
    assert_eq!(editor.ghost_text(), "el ");

    assert_eq!(editor.handle_key(key("tab")), PromptEditOutcome::Continue);
    assert_eq!(editor.text(), "/model ");
    type_text(&mut editor, "qwen");

    assert_eq!(
        editor.handle_key(key("enter")),
        PromptEditOutcome::Submit("/model qwen".to_owned())
    );
}

#[test]
fn prompt_editor_accepts_ghost_text_by_replacing_the_active_token() {
    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("model", "/model ", 10.0)],
    ))]);

    editor.insert_text("/mod");
    assert_eq!(editor.text(), "/mod");
    assert_eq!(editor.cursor(), 4);
    assert_eq!(editor.ghost_text(), "el ");

    assert!(editor.accept_ghost_text());
    assert_eq!(editor.text(), "/model ");
    assert_eq!(editor.cursor(), 7);
    assert!(editor.suggestions().is_empty());
    assert_eq!(editor.ghost_text(), "");
}

#[test]
fn prompt_editor_accepts_the_selected_suggestion_after_selection_moves() {
    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("top", "/top ", 20.0), item("second", "/second ", 10.0)],
    ))]);

    editor.insert_text("/");
    assert_eq!(ids(editor.suggestions()), vec!["top", "second"]);

    editor.move_selection(1);
    assert!(editor.accept_selected_suggestion());
    assert_eq!(editor.text(), "/second ");
    assert_eq!(editor.cursor(), 8);
}

#[test]
fn prompt_editor_refreshes_suggestions_after_cursor_and_delete_edits() {
    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "@",
        vec![item("file", "@file", 10.0)],
    ))]);

    editor.insert_text("inspect @fi");
    assert_eq!(ids(editor.suggestions()), vec!["file"]);

    editor.move_home();
    assert!(editor.suggestions().is_empty());

    editor.move_end();
    assert_eq!(ids(editor.suggestions()), vec!["file"]);

    editor.backspace();
    assert_eq!(editor.text(), "inspect @f");
    assert_eq!(ids(editor.suggestions()), vec!["file"]);

    editor.kill_to_start();
    assert_eq!(editor.text(), "");
    assert!(editor.suggestions().is_empty());
}

#[test]
fn prompt_editor_history_navigation_matches_python_prompt_input_priority() {
    let workspace = TestWorkspace::new("prompt-editor-history");
    let mut history = InputHistory::new(workspace.path().join("history.txt"));
    history.append("first prompt").expect("append first");
    history.append("second prompt").expect("append second");

    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("top", "/top ", 20.0), item("second", "/second ", 10.0)],
    ))]);

    type_text(&mut editor, "draft");
    assert_eq!(
        editor.handle_key_with_history(key("up"), &mut history),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.text(), "second prompt");
    assert!(editor.suggestions().is_empty());

    assert_eq!(
        editor.handle_key_with_history(key("up"), &mut history),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.text(), "first prompt");

    assert_eq!(
        editor.handle_key_with_history(key("down"), &mut history),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.text(), "second prompt");

    assert_eq!(
        editor.handle_key_with_history(key("down"), &mut history),
        PromptEditOutcome::Continue
    );
    assert_eq!(editor.text(), "draft");

    editor.set_text("");
    type_text(&mut editor, "/");
    assert_eq!(ids(editor.suggestions()), vec!["top", "second"]);
    assert_eq!(
        editor.handle_key_with_history(key("down"), &mut history),
        PromptEditOutcome::Continue
    );
    assert!(editor.accept_selected_suggestion());
    assert_eq!(editor.text(), "/second ");

    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("top", "/top ", 20.0), item("second", "/second ", 10.0)],
    ))]);
    type_text(&mut editor, "/");
    assert_eq!(
        editor.handle_key_with_history(key("up"), &mut history),
        PromptEditOutcome::Continue
    );
    assert!(editor.accept_selected_suggestion());
    assert_eq!(editor.text(), "/second ");
}

#[test]
fn prompt_editor_keybindings_match_python_prompt_input_resolution_order() {
    let mut bindings = KeybindingManager::new();
    bindings.register(KeyBinding::new("escape", "escape_action", "global"));
    bindings.register(KeyBinding::new("ctrl+r", "open_history_search", "global"));
    bindings.push_context("global");

    let mut editor = PromptEditor::new(vec![]);
    assert_eq!(
        editor.handle_key_with_bindings(key("escape"), &bindings),
        PromptEditOutcome::Action("escape_action".to_owned())
    );

    let mut editor = PromptEditor::new(vec![Box::new(StaticProvider::new(
        "/",
        vec![item("top", "/top ", 20.0), item("second", "/second ", 10.0)],
    ))]);
    type_text(&mut editor, "/");
    assert_eq!(
        editor.handle_key_with_bindings(key("escape"), &bindings),
        PromptEditOutcome::Continue
    );
    assert!(editor.suggestions().is_empty());

    editor.set_text("");
    type_text(&mut editor, "/");
    assert_eq!(
        editor.handle_key_with_bindings(ctrl("r"), &bindings),
        PromptEditOutcome::Action("open_history_search".to_owned())
    );
    assert_eq!(editor.text(), "/");
    assert!(editor.accept_selected_suggestion());
    assert_eq!(editor.text(), "/top ");
}

#[test]
fn prompt_editor_keybindings_precede_history_navigation_like_python_prompt_input() {
    let workspace = TestWorkspace::new("prompt-editor-keybinding-history");
    let mut history = InputHistory::new(workspace.path().join("history.txt"));
    history.append("old prompt").expect("append history");

    let mut bindings = KeybindingManager::new();
    bindings.register(KeyBinding::new("up", "custom_up", "global"));
    bindings.push_context("global");

    let mut editor = PromptEditor::new(vec![]);
    type_text(&mut editor, "draft");

    assert_eq!(
        editor.handle_key_with_history_and_bindings(key("up"), &mut history, &bindings),
        PromptEditOutcome::Action("custom_up".to_owned())
    );
    assert_eq!(editor.text(), "draft");
    assert!(!history.is_navigating());
}

fn item(id: &str, completion: &str, score: f64) -> SuggestionItem {
    SuggestionItem {
        id: id.to_owned(),
        display_text: id.to_owned(),
        completion: completion.to_owned(),
        description: None,
        icon: None,
        source: "test".to_owned(),
        score,
        arg_hint: None,
    }
}

fn ids(items: &[SuggestionItem]) -> Vec<&str> {
    items.iter().map(|item| item.id.as_str()).collect()
}

fn type_text(editor: &mut PromptEditor, text: &str) {
    for ch in text.chars() {
        assert_eq!(
            editor.handle_key(PromptKeyEvent::text(ch)),
            PromptEditOutcome::Continue
        );
    }
}

fn key(name: &str) -> PromptKeyEvent {
    PromptKeyEvent::new(name, "")
}

fn ctrl(name: &str) -> PromptKeyEvent {
    PromptKeyEvent::new(name, "").with_ctrl(true)
}

fn shift(name: &str) -> PromptKeyEvent {
    PromptKeyEvent::new(name, "").with_shift(true)
}

struct StaticProvider {
    trigger: &'static str,
    items: Vec<SuggestionItem>,
}

impl StaticProvider {
    fn new(trigger: &'static str, items: Vec<SuggestionItem>) -> Self {
        Self { trigger, items }
    }
}

impl SuggestionProvider for StaticProvider {
    fn trigger(&self) -> &str {
        self.trigger
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        self.items
            .iter()
            .filter(|item| {
                item.completion
                    .get(..token.text.len())
                    .is_some_and(|prefix| prefix.eq_ignore_ascii_case(&token.text))
            })
            .cloned()
            .collect()
    }
}

struct TestWorkspace {
    path: PathBuf,
}

impl TestWorkspace {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("iac-code-rs-tui-{name}-{unique}"));
        fs::create_dir_all(&path).expect("create test workspace");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.path).ok();
    }
}
