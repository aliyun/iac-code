use std::io;
use std::os::fd::RawFd;

use iac_code_config::credentials::{load_credentials, save_llm_key};
use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{
    get_active_provider_key, get_llm_source, get_provider_config, save_active_provider_config,
    save_llm_source,
};
use iac_code_providers::ProviderDescriptor;
use iac_code_tui::RawInputCapture;

use crate::cli_i18n::{tr, tr_dynamic, tr_value};
use crate::raw_auth::read_raw_auth_index_picker;
use crate::raw_auth_input::{read_raw_auth_masked_input, read_raw_auth_text_input};
use crate::raw_model_context::raw_model_provider_group_from_descriptor;
use crate::raw_model_effort::read_raw_auth_model_picker;
use crate::raw_prompt_context::RawPromptActionContext;

mod catalog;

use catalog::{
    raw_auth_configured_provider_message, raw_auth_current_label, raw_auth_is_partner_source,
    raw_auth_partner_sources, raw_auth_provider_groups, raw_auth_providers_for_group,
    RawAuthPartnerSource, RawAuthProviderGroup,
};
pub(super) use catalog::{
    raw_auth_configured_provider_model_message, raw_auth_llm_group_choice,
    raw_auth_provider_display_name, RawAuthLlmGroupChoice,
};

pub(super) fn read_raw_auth_llm_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    context: &RawPromptActionContext,
) -> io::Result<Option<String>> {
    let active_provider = get_active_provider_key(paths).ok().flatten();
    let current_llm_source = get_llm_source(paths).unwrap_or_else(|_| "local".to_owned());
    let partner_sources = raw_auth_partner_sources(&current_llm_source);
    let show_third_party = !partner_sources.is_empty();
    let groups = raw_auth_provider_groups();
    let mut group_options = Vec::new();
    if show_third_party {
        group_options.push(raw_auth_current_label(
            tr("Third-party"),
            raw_auth_is_partner_source(&current_llm_source),
        ));
    }
    group_options.extend(groups.iter().map(|group| {
        raw_auth_current_label(
            tr_dynamic(group.name),
            active_provider
                .as_deref()
                .is_some_and(|active| group.keys.contains(&active)),
        )
    }));
    let default_group_index = active_provider
        .as_deref()
        .and_then(|active| groups.iter().position(|group| group.keys.contains(&active)))
        .map(|index| index + usize::from(show_third_party))
        .or_else(|| raw_auth_is_partner_source(&current_llm_source).then_some(0))
        .unwrap_or(0);
    let Some(group_index) = read_raw_auth_index_picker(
        fd,
        capture,
        &tr("Select provider"),
        &group_options,
        default_group_index,
    )?
    else {
        return Ok(None);
    };
    let Some(group_choice) = raw_auth_llm_group_choice(show_third_party, group_index, groups.len())
    else {
        return Ok(None);
    };
    let group_index = match group_choice {
        RawAuthLlmGroupChoice::ThirdParty => {
            return read_raw_auth_partner_flow(
                fd,
                capture,
                paths,
                &partner_sources,
                &current_llm_source,
            );
        }
        RawAuthLlmGroupChoice::ProviderGroup(index) => index,
    };
    let group = &groups[group_index];
    let Some(provider) = read_raw_auth_provider_from_group(fd, capture, group, &active_provider)?
    else {
        return Ok(None);
    };

    configure_raw_auth_provider(fd, capture, paths, context, provider)
}

fn read_raw_auth_provider_from_group(
    fd: RawFd,
    capture: &RawInputCapture,
    group: &RawAuthProviderGroup,
    active_provider: &Option<String>,
) -> io::Result<Option<ProviderDescriptor>> {
    let providers = raw_auth_providers_for_group(group);
    if providers.is_empty() {
        return Ok(None);
    }

    if providers.len() == 1 {
        return Ok(providers.into_iter().next());
    }

    let provider_options = providers
        .iter()
        .map(|provider| {
            raw_auth_current_label(
                raw_auth_provider_display_name(provider),
                active_provider
                    .as_deref()
                    .is_some_and(|active| provider.key == active),
            )
        })
        .collect::<Vec<_>>();
    let default_provider_index = active_provider
        .as_deref()
        .and_then(|active| providers.iter().position(|provider| provider.key == active))
        .unwrap_or(0);
    let group_display_name = tr_dynamic(group.name);
    let provider_title = tr_value("Select provider — {group}", "group", &group_display_name);
    let Some(provider_index) = read_raw_auth_index_picker(
        fd,
        capture,
        &provider_title,
        &provider_options,
        default_provider_index,
    )?
    else {
        return Ok(None);
    };
    Ok(providers.get(provider_index).cloned())
}

fn configure_raw_auth_provider(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    context: &RawPromptActionContext,
    provider: ProviderDescriptor,
) -> io::Result<Option<String>> {
    let provider_display_name = raw_auth_provider_display_name(&provider);
    let api_base = read_raw_auth_api_base(fd, capture, paths, &provider, &provider_display_name)?;
    if !read_raw_auth_api_key(fd, capture, paths, &provider, &provider_display_name)? {
        return Ok(None);
    }

    let current_model = get_provider_config(paths, &provider.key)
        .ok()
        .and_then(|config| config.get("model").cloned())
        .unwrap_or_else(|| provider.default_model());
    let model_group = raw_model_provider_group_from_descriptor(&provider, &current_model);
    let model_context = RawPromptActionContext {
        model_initial_model: current_model,
        model_provider_groups: vec![model_group],
        ..context.clone()
    };
    let Some(selection) = read_raw_auth_model_picker(fd, capture, &model_context)? else {
        return Ok(None);
    };
    save_active_provider_config(paths, &provider.key, &selection.model, api_base.as_deref())
        .map_err(|error| io::Error::other(error.to_string()))?;
    Ok(Some(raw_auth_configured_provider_model_message(
        &provider_display_name,
        &selection.model,
    )))
}

fn read_raw_auth_api_base(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    provider: &ProviderDescriptor,
    provider_display_name: &str,
) -> io::Result<Option<String>> {
    if !matches!(
        provider.key.as_str(),
        "openapi_compatible" | "anthropic_compatible"
    ) {
        return Ok(None);
    }

    let existing_api_base = get_provider_config(paths, &provider.key)
        .ok()
        .and_then(|config| config.get("apiBase").cloned())
        .unwrap_or_else(|| "https://".to_owned());
    let Some(api_base) = read_raw_auth_text_input(
        fd,
        capture,
        &raw_auth_provider_title("Configure {provider}", provider_display_name),
        "API Base URL: ",
        &existing_api_base,
    )?
    else {
        return Ok(None);
    };
    let api_base = api_base.trim();
    if api_base.is_empty() {
        return Ok(None);
    }
    Ok(Some(api_base.to_owned()))
}

fn read_raw_auth_api_key(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    provider: &ProviderDescriptor,
    provider_display_name: &str,
) -> io::Result<bool> {
    if !provider.require_api_key {
        return Ok(true);
    }

    let existing_key = load_credentials(paths, None)
        .ok()
        .and_then(|credentials| credentials.get(&provider.key).cloned())
        .unwrap_or_default();
    let Some(api_key) = read_raw_auth_masked_input(
        fd,
        capture,
        &raw_auth_provider_title("Enter API key for {provider}", provider_display_name),
        "API key: ",
        &existing_key,
    )?
    else {
        return Ok(false);
    };
    let api_key = api_key.trim();
    if api_key.is_empty() {
        return Ok(false);
    }
    if api_key != existing_key {
        save_llm_key(paths, &provider.key, api_key)
            .map_err(|error| io::Error::other(error.to_string()))?;
    }
    Ok(true)
}

fn read_raw_auth_partner_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    paths: &ConfigPaths,
    partner_sources: &[RawAuthPartnerSource],
    current_llm_source: &str,
) -> io::Result<Option<String>> {
    let options = partner_sources
        .iter()
        .map(|source| {
            raw_auth_current_label(
                tr_dynamic(source.display_name),
                source.key == current_llm_source,
            )
        })
        .collect::<Vec<_>>();
    let default_index = partner_sources
        .iter()
        .position(|source| source.key == current_llm_source)
        .unwrap_or(0);
    let title = tr_value("Select provider — {group}", "group", &tr("Third-party"));
    let Some(index) = read_raw_auth_index_picker(fd, capture, &title, &options, default_index)?
    else {
        return Ok(None);
    };
    let source = &partner_sources[index];
    save_llm_source(paths, source.key).map_err(|error| io::Error::other(error.to_string()))?;
    Ok(Some(raw_auth_configured_provider_message(&tr_dynamic(
        source.display_name,
    ))))
}

fn raw_auth_provider_title(message: &'static str, provider_display_name: &str) -> String {
    tr_value(message, "provider", provider_display_name)
}
