use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum EffortLevel {
    Low,
    Medium,
    High,
    XHigh,
    Max,
    Auto,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelThinkingSpec {
    allowed_efforts: Vec<EffortLevel>,
    default_effort: Option<EffortLevel>,
}

impl ModelThinkingSpec {
    pub fn none() -> Self {
        Self {
            allowed_efforts: Vec::new(),
            default_effort: None,
        }
    }

    pub fn new(allowed_efforts: Vec<EffortLevel>, default_effort: Option<EffortLevel>) -> Self {
        Self {
            allowed_efforts,
            default_effort,
        }
    }

    pub fn supports_effort(&self) -> bool {
        !self.allowed_efforts.is_empty()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelDefinition {
    pub model: String,
    pub thinking: ModelThinkingSpec,
}

impl ModelDefinition {
    pub fn new(model: impl Into<String>, thinking: ModelThinkingSpec) -> Self {
        Self {
            model: model.into(),
            thinking,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelProviderGroup {
    pub provider_key: String,
    pub display_name: String,
    pub models: Vec<ModelDefinition>,
}

impl ModelProviderGroup {
    pub fn new(
        provider_key: impl Into<String>,
        display_name: impl Into<String>,
        models: Vec<ModelDefinition>,
    ) -> Self {
        Self {
            provider_key: provider_key.into(),
            display_name: display_name.into(),
            models,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ModelPickerEntry {
    Header { display_name: String },
    Model { provider_key: String, model: String },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelSelection {
    pub provider_key: String,
    pub model: String,
    pub effort: Option<EffortLevel>,
}

pub struct ModelPickerState {
    items: Vec<ModelPickerEntry>,
    specs: BTreeMap<(String, String), ModelThinkingSpec>,
    efforts: BTreeMap<(String, String), EffortLevel>,
    changed_efforts: BTreeSet<(String, String)>,
    focused_index: usize,
    done: bool,
    result: Option<ModelSelection>,
}

impl ModelPickerState {
    pub fn new(initial_model: &str, groups: Vec<ModelProviderGroup>) -> Self {
        let mut items = Vec::new();
        let mut specs = BTreeMap::new();
        let mut efforts = BTreeMap::new();

        for group in groups {
            items.push(ModelPickerEntry::Header {
                display_name: group.display_name,
            });
            for definition in group.models {
                let key = (group.provider_key.clone(), definition.model.clone());
                if definition.thinking.supports_effort() {
                    if let Some(default_effort) = definition.thinking.default_effort {
                        efforts.insert(key.clone(), default_effort);
                    }
                }
                specs.insert(key, definition.thinking);
                items.push(ModelPickerEntry::Model {
                    provider_key: group.provider_key.clone(),
                    model: definition.model,
                });
            }
        }

        let focused_index = initial_focus(&items, initial_model);
        Self {
            items,
            specs,
            efforts,
            changed_efforts: BTreeSet::new(),
            focused_index,
            done: false,
            result: None,
        }
    }

    pub fn items(&self) -> &[ModelPickerEntry] {
        &self.items
    }

    pub fn focused_index(&self) -> usize {
        self.focused_index
    }

    pub fn focused_pair(&self) -> Option<(&str, &str)> {
        match self.items.get(self.focused_index)? {
            ModelPickerEntry::Header { .. } => None,
            ModelPickerEntry::Model {
                provider_key,
                model,
            } => Some((provider_key.as_str(), model.as_str())),
        }
    }

    pub fn effort_for(&self, provider_key: &str, model: &str) -> Option<EffortLevel> {
        self.efforts
            .get(&(provider_key.to_owned(), model.to_owned()))
            .copied()
    }

    pub fn is_done(&self) -> bool {
        self.done
    }

    pub fn result(&self) -> Option<&ModelSelection> {
        self.result.as_ref()
    }

    pub fn move_focus(&mut self, direction: isize) {
        if self.items.is_empty() || direction == 0 {
            return;
        }

        let step = if direction > 0 { 1 } else { -1 };
        let mut index = self.focused_index as isize + step;
        while index >= 0 && (index as usize) < self.items.len() {
            if matches!(self.items[index as usize], ModelPickerEntry::Model { .. }) {
                self.focused_index = index as usize;
                return;
            }
            index += step;
        }
    }

    pub fn cycle_effort(&mut self, pair: (&str, &str), direction: isize) {
        let key = (pair.0.to_owned(), pair.1.to_owned());
        let Some(spec) = self.specs.get(&key) else {
            return;
        };
        if !spec.supports_effort() {
            return;
        }

        let allowed = &spec.allowed_efforts;
        let fallback = spec
            .default_effort
            .filter(|value| allowed.contains(value))
            .unwrap_or(allowed[0]);
        let current = self.efforts.get(&key).copied().unwrap_or(fallback);
        let current_index = allowed
            .iter()
            .position(|value| *value == current)
            .unwrap_or_else(|| {
                allowed
                    .iter()
                    .position(|value| *value == fallback)
                    .unwrap_or(0)
            });
        let next_index = current_index
            .saturating_add_signed(direction)
            .min(allowed.len() - 1);
        let next = allowed[next_index];
        if next != current {
            self.changed_efforts.insert(key.clone());
        }
        self.efforts.insert(key, next);
    }

    pub fn select_focused(&mut self) -> Option<ModelSelection> {
        let (provider_key, model) = self.focused_pair()?;
        let provider_key = provider_key.to_owned();
        let model = model.to_owned();
        let spec = self.specs.get(&(provider_key.clone(), model.clone()));
        let effort = if spec.is_some_and(ModelThinkingSpec::supports_effort)
            && self
                .changed_efforts
                .contains(&(provider_key.clone(), model.clone()))
        {
            self.effort_for(&provider_key, &model)
        } else {
            None
        };
        let selection = ModelSelection {
            provider_key,
            model,
            effort,
        };
        self.done = true;
        self.result = Some(selection.clone());
        Some(selection)
    }

    pub fn cancel(&mut self) {
        self.done = true;
    }
}

fn initial_focus(items: &[ModelPickerEntry], initial_model: &str) -> usize {
    for (index, item) in items.iter().enumerate() {
        if matches!(item, ModelPickerEntry::Model { model, .. } if model == initial_model) {
            return index;
        }
    }
    items
        .iter()
        .position(|item| matches!(item, ModelPickerEntry::Model { .. }))
        .unwrap_or(0)
}
