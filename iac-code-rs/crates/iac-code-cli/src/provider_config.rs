use iac_code_config::credentials::load_credentials;
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{
    get_active_provider_key, get_llm_source, get_provider_config, load_saved_model, DEFAULT_MODEL,
};
use iac_code_providers::{create_provider_config, load_from_qwenpaw, ConfiguredProvider};

use super::cli_i18n::{tr, tr_value};

pub(super) fn load_configured_provider(
    paths: &ConfigPaths,
    cli_model: &str,
) -> Result<(ConfiguredProvider, String), String> {
    let qwenpaw_config = if get_llm_source(paths).map_err(|error| error.to_string())? == "qwenpaw" {
        load_from_qwenpaw()?
    } else {
        None
    };
    let provider_key = if let Some(config) = &qwenpaw_config {
        Some(config.provider_key.clone())
    } else {
        get_active_provider_key(paths).map_err(|error| error.to_string())?
    };
    let initial_provider_settings = match provider_key.as_deref() {
        Some(key) => get_provider_config(paths, key).map_err(|error| error.to_string())?,
        None => Default::default(),
    };
    let model = if cli_model.trim().is_empty() {
        qwenpaw_config
            .as_ref()
            .map(|config| config.model.clone())
            .or(load_saved_model(paths).map_err(|error| error.to_string())?)
            .unwrap_or_else(|| DEFAULT_MODEL.to_owned())
    } else {
        cli_model.trim().to_owned()
    };
    let mut credentials =
        load_credentials(paths, Some(&model)).map_err(|error| error.to_string())?;
    if let Some(config) = &qwenpaw_config {
        if let Some(api_key) = &config.api_key {
            credentials.insert(config.provider_key.clone(), api_key.clone());
        }
    }
    let base_url_override = qwenpaw_config
        .as_ref()
        .and_then(|config| config.base_url.as_deref());
    let saved_base_url = initial_provider_settings.get("apiBase").map(String::as_str);
    let mut provider_config = create_provider_config(
        &model,
        &credentials,
        provider_key.as_deref(),
        base_url_override,
        saved_base_url,
    )
    .map_err(|error| localize_provider_config_error(&error))?;
    let provider_settings = get_provider_config(paths, &provider_config.provider_key)
        .map_err(|error| error.to_string())?;
    if provider_config.base_url.is_none() {
        provider_config.base_url = provider_settings.get("apiBase").cloned();
    }
    provider_config.effort = provider_settings.get("effort").cloned();
    Ok((ConfiguredProvider::new(provider_config), model))
}

fn localize_provider_config_error(error: &str) -> String {
    const CANNOT_DETERMINE_PREFIX: &str = "Cannot determine provider for model: ";
    const CONFIGURE_SUFFIX: &str = ". Run /auth to configure.";
    if let Some(model) = error
        .strip_prefix(CANNOT_DETERMINE_PREFIX)
        .and_then(|value| value.strip_suffix(CONFIGURE_SUFFIX))
    {
        return tr_value(
            "Cannot determine provider for model: {model}. Run /auth to configure.",
            "model",
            model,
        );
    }

    if let Some(key) = error
        .strip_prefix("Unknown provider key: '")
        .and_then(|value| value.strip_suffix("'. Run /auth to configure."))
    {
        return tr_value(
            "Unknown provider key: '{key}'. Run /auth to configure.",
            "key",
            key,
        );
    }

    if let Some(rest) = error.strip_prefix("No API key configured for provider '") {
        if let Some((provider, model_part)) = rest.split_once("' (model: ") {
            if let Some(model) = model_part.strip_suffix("). Run /auth to configure.") {
                return tr("No API key configured for provider '{provider}' (model: {model}). Run /auth to configure.")
                    .replace("{provider}", provider)
                    .replace("{model}", model);
            }
        }
    }

    error.to_owned()
}
