#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KeyBinding {
    pub key: String,
    pub action: String,
    pub context: String,
    pub consumes: bool,
}

impl KeyBinding {
    pub fn new(
        key: impl Into<String>,
        action: impl Into<String>,
        context: impl Into<String>,
    ) -> Self {
        Self {
            key: key.into(),
            action: action.into(),
            context: context.into(),
            consumes: true,
        }
    }

    pub fn with_consumes(mut self, consumes: bool) -> Self {
        self.consumes = consumes;
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KeyBindingId(usize);

#[derive(Default, Debug)]
pub struct KeybindingManager {
    bindings: Vec<(KeyBindingId, KeyBinding)>,
    context_stack: Vec<String>,
    next_id: usize,
}

pub fn default_global_keybinding_manager() -> KeybindingManager {
    let mut manager = KeybindingManager::new();
    register_default_global_keybindings(&mut manager);
    manager.push_context("global");
    manager
}

pub fn register_default_global_keybindings(manager: &mut KeybindingManager) {
    for (key, action) in [
        ("ctrl+r", "open_history_search"),
        ("ctrl+p", "open_quick_open"),
        ("ctrl+f", "open_global_search"),
        ("ctrl+o", "expand_last_turn"),
        ("ctrl+v", "paste_image"),
    ] {
        manager.register(KeyBinding::new(key, action, "global"));
    }
}

impl KeybindingManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, binding: KeyBinding) -> KeyBindingId {
        let id = KeyBindingId(self.next_id);
        self.next_id += 1;
        self.bindings.push((id, binding));
        id
    }

    pub fn unregister(&mut self, id: KeyBindingId) -> bool {
        let before = self.bindings.len();
        self.bindings.retain(|(binding_id, _)| *binding_id != id);
        self.bindings.len() != before
    }

    pub fn unregister_context(&mut self, context: &str) {
        self.bindings
            .retain(|(_, binding)| binding.context != context);
    }

    pub fn push_context(&mut self, context: impl Into<String>) {
        self.context_stack.push(context.into());
    }

    pub fn pop_context(&mut self, context: &str) {
        if let Some(index) = self
            .context_stack
            .iter()
            .rposition(|candidate| candidate == context)
        {
            self.context_stack.remove(index);
        }
    }

    pub fn active_contexts(&self) -> &[String] {
        &self.context_stack
    }

    pub fn resolve(&self, event: &crate::PromptKeyEvent) -> Option<String> {
        let key_id = event.key_id();
        for context in self.context_stack.iter().rev() {
            for (_, binding) in self
                .bindings
                .iter()
                .filter(|(_, binding)| &binding.context == context)
            {
                if binding.key == key_id && binding.consumes {
                    return Some(binding.action.clone());
                }
                if binding.key == key_id {
                    break;
                }
            }
        }
        None
    }

    pub fn get_display_text(&self, action: &str, context: &str) -> Option<String> {
        self.bindings
            .iter()
            .find(|(_, binding)| binding.action == action && binding.context == context)
            .map(|(_, binding)| format_key_display(&binding.key))
    }

    pub fn get_hints_for_context(&self, context: &str) -> Vec<(String, String)> {
        self.bindings
            .iter()
            .filter(|(_, binding)| binding.context == context)
            .map(|(_, binding)| (format_key_display(&binding.key), binding.action.clone()))
            .collect()
    }
}

fn format_key_display(key_id: &str) -> String {
    key_id
        .split('+')
        .map(|part| {
            let mut chars = part.chars();
            let Some(first) = chars.next() else {
                return String::new();
            };
            format!("{}{}", first.to_uppercase(), chars.as_str())
        })
        .collect::<Vec<_>>()
        .join("+")
}
