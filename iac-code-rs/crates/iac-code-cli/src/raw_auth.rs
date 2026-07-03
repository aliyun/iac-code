use std::io;
use std::os::fd::RawFd;

use iac_code_tui::RawInputCapture;

use crate::cli_i18n::tr;
use crate::raw_auth_cloud::read_raw_auth_cloud_flow;
#[cfg(test)]
pub(super) use crate::raw_auth_llm::raw_auth_configured_provider_model_message;
pub(super) use crate::raw_auth_llm::raw_auth_provider_display_name;
use crate::raw_auth_llm::read_raw_auth_llm_flow;
use crate::raw_prompt_context::RawPromptActionContext;
#[cfg(test)]
use crate::raw_select::raw_select_render_output;
use crate::raw_select::{read_raw_select_index, read_raw_select_index_with_info, RawSelectOption};
use crate::raw_transcript::RawAlternateScreenGuard;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum RawAuthConfigurationChoice {
    LlmProvider,
    IacCloudService,
}

pub(super) fn raw_auth_configuration_choice(index: usize) -> Option<RawAuthConfigurationChoice> {
    match index {
        0 => Some(RawAuthConfigurationChoice::LlmProvider),
        1 => Some(RawAuthConfigurationChoice::IacCloudService),
        _ => None,
    }
}

#[cfg(unix)]
pub(super) fn raw_auth_label(message: &'static str) -> String {
    format!("{}: ", tr(message))
}

#[cfg(unix)]
pub(super) fn read_raw_auth_flow(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<String>> {
    let Some(paths) = &context.config_paths else {
        return Ok(None);
    };
    let _screen = RawAlternateScreenGuard::enter(fd)?;

    let configuration_options = vec![
        tr("Configure LLM Provider"),
        tr("Configure IaC Cloud Service"),
    ];
    let Some(configuration_index) = read_raw_auth_index_picker(
        fd,
        capture,
        &tr("Select configuration type"),
        &configuration_options,
        0,
    )?
    else {
        return Ok(None);
    };
    match raw_auth_configuration_choice(configuration_index) {
        Some(RawAuthConfigurationChoice::LlmProvider) => {
            read_raw_auth_llm_flow(fd, capture, paths, context)
        }
        Some(RawAuthConfigurationChoice::IacCloudService) => {
            read_raw_auth_cloud_flow(fd, capture, paths)
        }
        None => Ok(None),
    }
}

#[cfg(unix)]
pub(super) fn read_raw_auth_index_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    options: &[String],
    default_index: usize,
) -> io::Result<Option<usize>> {
    let select_options = options
        .iter()
        .cloned()
        .map(RawSelectOption::new)
        .collect::<Vec<_>>();
    read_raw_select_index(fd, capture, title, &select_options, default_index)
}

#[cfg(unix)]
pub(super) fn read_raw_auth_index_picker_with_info(
    fd: RawFd,
    capture: &RawInputCapture,
    title: &str,
    info_lines: &[String],
    options: &[String],
    default_index: usize,
) -> io::Result<Option<usize>> {
    let select_options = options
        .iter()
        .cloned()
        .map(RawSelectOption::new)
        .collect::<Vec<_>>();
    read_raw_select_index_with_info(
        fd,
        capture,
        title,
        info_lines,
        &select_options,
        default_index,
    )
}

#[cfg(all(test, unix))]
pub(super) fn raw_auth_index_picker_render_output(
    previous_lines: usize,
    title: &str,
    options: &[String],
    focused: usize,
    width: usize,
) -> (String, usize) {
    let select_options = options
        .iter()
        .cloned()
        .map(RawSelectOption::new)
        .collect::<Vec<_>>();
    raw_select_render_output(previous_lines, title, &select_options, focused, width)
}
