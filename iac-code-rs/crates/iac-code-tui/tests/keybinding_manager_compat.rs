use iac_code_tui::{
    default_global_keybinding_manager, KeyBinding, KeybindingManager, PromptKeyEvent,
};

#[test]
fn keybinding_manager_registers_and_resolves_consuming_bindings() {
    let mut manager = KeybindingManager::new();
    manager.register(KeyBinding::new("ctrl+r", "reload", "global"));
    manager.push_context("global");

    let resolved = manager.resolve(&PromptKeyEvent::new("r", "").with_ctrl(true));

    assert_eq!(resolved.as_deref(), Some("reload"));
}

#[test]
fn keybinding_manager_resolves_context_priority_and_bubbling_like_python() {
    let mut manager = KeybindingManager::new();
    manager.register(KeyBinding::new("escape", "cancel_global", "global"));
    manager.register(KeyBinding::new("escape", "cancel_dialog", "dialog"));
    manager.push_context("global");
    manager.push_context("dialog");

    assert_eq!(
        manager
            .resolve(&PromptKeyEvent::new("escape", ""))
            .as_deref(),
        Some("cancel_dialog")
    );

    let mut manager = KeybindingManager::new();
    manager.register(KeyBinding::new("escape", "cancel_global", "global"));
    manager.register(KeyBinding::new("escape", "cancel_dialog", "dialog").with_consumes(false));
    manager.push_context("global");
    manager.push_context("dialog");

    assert_eq!(
        manager
            .resolve(&PromptKeyEvent::new("escape", ""))
            .as_deref(),
        Some("cancel_global")
    );
}

#[test]
fn keybinding_manager_unregisters_bindings_and_contexts() {
    let mut manager = KeybindingManager::new();
    let binding_id = manager.register(KeyBinding::new("ctrl+r", "reload", "global"));
    manager.push_context("global");

    assert_eq!(
        manager
            .resolve(&PromptKeyEvent::new("r", "").with_ctrl(true))
            .as_deref(),
        Some("reload")
    );

    assert!(manager.unregister(binding_id));
    assert!(manager
        .resolve(&PromptKeyEvent::new("r", "").with_ctrl(true))
        .is_none());

    manager.register(KeyBinding::new("escape", "cancel", "global"));
    manager.unregister_context("global");
    assert!(manager
        .resolve(&PromptKeyEvent::new("escape", ""))
        .is_none());
}

#[test]
fn keybinding_manager_tracks_context_stack_like_python() {
    let mut manager = KeybindingManager::new();
    assert!(manager.active_contexts().is_empty());

    manager.push_context("global");
    manager.push_context("dialog");
    manager.push_context("global");
    assert_eq!(manager.active_contexts(), &["global", "dialog", "global"]);

    manager.pop_context("global");
    assert_eq!(manager.active_contexts(), &["global", "dialog"]);

    manager.pop_context("missing");
    assert_eq!(manager.active_contexts(), &["global", "dialog"]);
}

#[test]
fn keybinding_manager_formats_display_text_and_hints_like_python() {
    let mut manager = KeybindingManager::new();
    manager.register(KeyBinding::new("ctrl+r", "reload", "global"));
    manager.register(KeyBinding::new("escape", "cancel", "global"));
    manager.register(KeyBinding::new("ctrl+alt+x", "special", "global"));

    assert_eq!(
        manager.get_display_text("reload", "global").as_deref(),
        Some("Ctrl+R")
    );
    assert_eq!(
        manager.get_display_text("special", "global").as_deref(),
        Some("Ctrl+Alt+X")
    );
    assert!(manager.get_display_text("missing", "global").is_none());

    assert_eq!(
        manager.get_hints_for_context("global"),
        vec![
            ("Ctrl+R".to_owned(), "reload".to_owned()),
            ("Escape".to_owned(), "cancel".to_owned()),
            ("Ctrl+Alt+X".to_owned(), "special".to_owned()),
        ]
    );
}

#[test]
fn default_global_keybindings_match_python_repl_registration() {
    let manager = default_global_keybinding_manager();

    for (key, action) in [
        (
            PromptKeyEvent::new("r", "").with_ctrl(true),
            "open_history_search",
        ),
        (
            PromptKeyEvent::new("p", "").with_ctrl(true),
            "open_quick_open",
        ),
        (
            PromptKeyEvent::new("f", "").with_ctrl(true),
            "open_global_search",
        ),
        (
            PromptKeyEvent::new("o", "").with_ctrl(true),
            "expand_last_turn",
        ),
        (PromptKeyEvent::new("v", "").with_ctrl(true), "paste_image"),
    ] {
        assert_eq!(manager.resolve(&key).as_deref(), Some(action));
    }

    assert_eq!(
        manager.get_hints_for_context("global"),
        vec![
            ("Ctrl+R".to_owned(), "open_history_search".to_owned()),
            ("Ctrl+P".to_owned(), "open_quick_open".to_owned()),
            ("Ctrl+F".to_owned(), "open_global_search".to_owned()),
            ("Ctrl+O".to_owned(), "expand_last_turn".to_owned()),
            ("Ctrl+V".to_owned(), "paste_image".to_owned()),
        ]
    );
}
