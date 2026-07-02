use iac_code_config::credentials::save_llm_key;
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{
    get_active_provider_key, get_llm_source, load_active_provider_effort, load_saved_model,
    partner_source_display_name, resolve_provider_key, save_active_provider_config,
    save_active_provider_effort, save_active_provider_model,
};

use crate::cli_i18n::{tr, tr_value};
use crate::interactive_commands::print_interactive_command_result;
#[cfg(unix)]
use crate::raw_effort::effort_spec;

pub(super) fn print_interactive_auth(args: &str) {
    let message = match interactive_auth_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn interactive_auth_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let parts = args.split_whitespace().collect::<Vec<_>>();
    match parts.as_slice() {
        [] => {
            let Some(provider_key) =
                get_active_provider_key(&paths).map_err(|error| error.to_string())?
            else {
                return Ok("No active provider configured.".to_owned());
            };
            let model = load_saved_model(&paths)
                .map_err(|error| error.to_string())?
                .unwrap_or_default();
            Ok(format!("Active provider: {provider_key}\nModel: {model}"))
        }
        [provider, model, api_key] | [provider, model, api_key, _] => {
            let provider_key = resolve_provider_key(provider).map_err(|error| error.to_string())?;
            let api_base = parts.get(3).copied();
            save_llm_key(&paths, &provider_key, api_key).map_err(|error| error.to_string())?;
            save_active_provider_config(&paths, &provider_key, model, api_base)
                .map_err(|error| error.to_string())?;
            Ok(format!(
                "Authentication configured: {provider_key} / {model}"
            ))
        }
        _ => Ok("Usage: /auth <provider> <model> <api-key> [api-base]".to_owned()),
    }
}

pub(super) fn print_interactive_model(args: &str) {
    let message = match interactive_model_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn interactive_model_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let llm_source = get_llm_source(&paths).map_err(|error| error.to_string())?;
    if llm_source != "local" {
        let display_name = partner_source_display_name(&llm_source);
        return Ok(tr_value(
            "Model is managed by '{source}'. To change model, modify it in {source} or switch provider via /auth.",
            "source",
            display_name,
        ));
    }
    let parts = args.split_whitespace().collect::<Vec<_>>();
    if let Some(model) = parts.first() {
        let Some(provider_key) =
            get_active_provider_key(&paths).map_err(|error| error.to_string())?
        else {
            return Ok(tr("No configured providers. Run /auth first."));
        };
        save_active_provider_model(&paths, model).map_err(|error| error.to_string())?;
        save_valid_model_effort_arg(&paths, &provider_key, model, parts.get(1).copied())?;
        Ok(tr_value("Model switched to: {model}", "model", model))
    } else {
        let model = load_saved_model(&paths)
            .map_err(|error| error.to_string())?
            .unwrap_or_default();
        Ok(tr_value("Current model: {model}", "model", &model))
    }
}

#[cfg(unix)]
fn save_valid_model_effort_arg(
    paths: &ConfigPaths,
    provider_key: &str,
    model: &str,
    effort: Option<&str>,
) -> Result<(), String> {
    let Some(effort) = effort.map(|value| value.trim().to_ascii_lowercase()) else {
        return Ok(());
    };
    let Some((allowed_efforts, _default_effort)) = effort_spec(provider_key, model) else {
        return Ok(());
    };
    if allowed_efforts.contains(&effort.as_str()) {
        save_active_provider_effort(paths, &effort).map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn save_valid_model_effort_arg(
    _paths: &ConfigPaths,
    _provider_key: &str,
    _model: &str,
    _effort: Option<&str>,
) -> Result<(), String> {
    Ok(())
}

pub(super) fn print_interactive_effort(args: &str) {
    let message = match interactive_effort_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

#[cfg(unix)]
fn interactive_effort_message(args: &str) -> Result<String, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let Some(provider_key) = get_active_provider_key(&paths).map_err(|error| error.to_string())?
    else {
        return Ok(tr("No configured providers. Run /auth first."));
    };
    let Some(model) = load_saved_model(&paths).map_err(|error| error.to_string())? else {
        return Ok(tr("No model selected. Run /model first."));
    };
    let Some((allowed_efforts, default_effort)) = effort_spec(&provider_key, &model) else {
        return Ok(tr_value(
            "Model {model} does not support effort.",
            "model",
            &model,
        ));
    };
    let parts = args.split_whitespace().collect::<Vec<_>>();
    if let Some(effort) = parts.first() {
        let effort = effort.trim().to_ascii_lowercase();
        if !allowed_efforts.contains(&effort.as_str()) {
            return Ok(tr_value(
                "Invalid effort. Allowed: {labels}",
                "labels",
                &allowed_efforts.join(", "),
            ));
        }
        save_active_provider_effort(&paths, &effort).map_err(|error| error.to_string())?;
        Ok(tr_value("Effort switched to: {effort}", "effort", &effort))
    } else {
        let current = load_active_provider_effort(&paths)
            .map_err(|error| error.to_string())?
            .map(|effort| effort.trim().to_ascii_lowercase())
            .filter(|effort| allowed_efforts.contains(&effort.as_str()))
            .unwrap_or_else(|| default_effort.to_owned());
        Ok(tr_value("Current effort: {effort}", "effort", &current))
    }
}

#[cfg(not(unix))]
fn interactive_effort_message(_args: &str) -> Result<String, String> {
    Ok(tr("Effort selection is unavailable on this platform."))
}
