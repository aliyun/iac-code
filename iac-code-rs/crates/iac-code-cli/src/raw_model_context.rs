use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{get_active_provider_key, get_llm_source, load_saved_model};
use iac_code_providers::{provider_descriptor, ProviderDescriptor};
use iac_code_tui::{ModelDefinition, ModelProviderGroup};

use crate::raw_auth::raw_auth_provider_display_name;
use crate::raw_effort::raw_model_thinking_spec;

pub(super) fn raw_model_provider_group_from_descriptor(
    provider: &ProviderDescriptor,
    initial_model: &str,
) -> ModelProviderGroup {
    let mut models = provider
        .models
        .iter()
        .map(|entry| {
            ModelDefinition::new(
                entry.id.clone(),
                raw_model_thinking_spec(&provider.key, &entry.id),
            )
        })
        .collect::<Vec<_>>();
    if !initial_model.is_empty() && !models.iter().any(|entry| entry.model == initial_model) {
        models.insert(
            0,
            ModelDefinition::new(
                initial_model.to_owned(),
                raw_model_thinking_spec(&provider.key, initial_model),
            ),
        );
    }
    ModelProviderGroup::new(
        provider.key.clone(),
        raw_auth_provider_display_name(provider),
        models,
    )
}

pub(super) fn raw_model_picker_context(paths: &ConfigPaths) -> (String, Vec<ModelProviderGroup>) {
    let llm_source = get_llm_source(paths).unwrap_or_else(|_| "local".to_owned());
    if llm_source != "local" {
        return (String::new(), Vec::new());
    }
    let Some(provider_key) = get_active_provider_key(paths).ok().flatten() else {
        return (String::new(), Vec::new());
    };
    let Some(descriptor) = provider_descriptor(&provider_key) else {
        return (String::new(), Vec::new());
    };
    let initial_model = load_saved_model(paths)
        .ok()
        .flatten()
        .filter(|model| !model.trim().is_empty())
        .unwrap_or_else(|| descriptor.default_model());
    let mut models = descriptor
        .models
        .iter()
        .map(|entry| {
            ModelDefinition::new(
                entry.id.clone(),
                raw_model_thinking_spec(&provider_key, &entry.id),
            )
        })
        .collect::<Vec<_>>();
    if !initial_model.is_empty() && !models.iter().any(|entry| entry.model == initial_model) {
        models.insert(
            0,
            ModelDefinition::new(
                initial_model.clone(),
                raw_model_thinking_spec(&provider_key, &initial_model),
            ),
        );
    }
    if models.is_empty() {
        return (initial_model, Vec::new());
    }
    (
        initial_model,
        vec![ModelProviderGroup::new(
            provider_key,
            descriptor.display_name,
            models,
        )],
    )
}
