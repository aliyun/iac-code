use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;

use iac_code_tui::{ModelPickerEntry, ModelPickerState, ModelSelection, RawInputCapture};

use super::cli_i18n::{tr, tr_value};
use super::raw_auth_input::read_raw_auth_text_input;
use super::raw_effort::effort_level_symbol;
use super::raw_picker::{
    clear_raw_picker, raw_picker_terminal_width, write_raw_interactive_fd_all,
};
use super::raw_prompt_context::RawPromptActionContext;
use super::raw_select::{raw_select_initial_focus, raw_select_render_output, RawSelectOption};
use super::raw_transcript::RawAlternateScreenGuard;

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
struct RawModelPickerOptions {
    enter_alternate_screen: bool,
    include_custom_model: bool,
    fallback_to_custom_when_empty: bool,
    allow_effort_cycle: bool,
}

#[cfg(unix)]
pub(super) fn read_raw_auth_model_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<ModelSelection>> {
    read_raw_model_selection(
        fd,
        capture,
        context,
        RawModelPickerOptions {
            enter_alternate_screen: false,
            include_custom_model: true,
            fallback_to_custom_when_empty: true,
            allow_effort_cycle: false,
        },
    )
}

#[cfg(unix)]
fn raw_auth_model_picker_title(state: &ModelPickerState) -> String {
    state
        .items()
        .iter()
        .find_map(|item| match item {
            ModelPickerEntry::Header { display_name } => Some(display_name.as_str()),
            ModelPickerEntry::Model { .. } => None,
        })
        .map(|provider| tr_value("Select model for {provider}", "provider", provider))
        .unwrap_or_else(|| tr("Select model"))
}

#[cfg(unix)]
fn raw_auth_model_last_index(state: &ModelPickerState) -> usize {
    state
        .items()
        .iter()
        .enumerate()
        .rev()
        .find_map(|(index, item)| matches!(item, ModelPickerEntry::Model { .. }).then_some(index))
        .unwrap_or_else(|| state.focused_index())
}

#[cfg(unix)]
pub(super) fn read_raw_model_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<ModelSelection>> {
    read_raw_model_selection(
        fd,
        capture,
        context,
        RawModelPickerOptions {
            enter_alternate_screen: true,
            include_custom_model: true,
            fallback_to_custom_when_empty: false,
            allow_effort_cycle: true,
        },
    )
}

#[cfg(unix)]
fn read_raw_model_selection(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
    options: RawModelPickerOptions,
) -> io::Result<Option<ModelSelection>> {
    let mut state = ModelPickerState::new(
        &context.model_initial_model,
        context.model_provider_groups.clone(),
    );
    if !state
        .items()
        .iter()
        .any(|item| matches!(item, ModelPickerEntry::Model { .. }))
    {
        if !options.fallback_to_custom_when_empty {
            return Ok(None);
        }
        return read_custom_model_selection(fd, capture, &state, first_provider_key(context));
    }
    let _screen = if options.enter_alternate_screen {
        Some(RawAlternateScreenGuard::enter(fd)?)
    } else {
        None
    };

    let mut custom_focused = false;
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_model_selection(
        fd,
        rendered_lines,
        &state,
        &context.model_initial_model,
        options.include_custom_model,
        custom_focused,
    )?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            if custom_focused && options.include_custom_model {
                clear_raw_picker(fd, rendered_lines)?;
                return read_custom_model_selection(
                    fd,
                    capture,
                    &state,
                    first_provider_key(context),
                );
            }
            let selected = state.select_focused();
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            if custom_focused {
                custom_focused = false;
            } else {
                state.move_focus(-1);
            }
            rendered_lines = render_raw_model_selection(
                fd,
                rendered_lines,
                &state,
                &context.model_initial_model,
                options.include_custom_model,
                custom_focused,
            )?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            if options.include_custom_model
                && !custom_focused
                && state.focused_index() == raw_auth_model_last_index(&state)
            {
                custom_focused = true;
            } else if !custom_focused {
                state.move_focus(1);
            }
            rendered_lines = render_raw_model_selection(
                fd,
                rendered_lines,
                &state,
                &context.model_initial_model,
                options.include_custom_model,
                custom_focused,
            )?;
            continue;
        }
        if options.allow_effort_cycle && (key == "left" || key == "right") && !custom_focused {
            let direction = if key == "left" { -1 } else { 1 };
            if let Some((provider_key, model)) = state
                .focused_pair()
                .map(|(provider_key, model)| (provider_key.to_owned(), model.to_owned()))
            {
                state.cycle_effort((&provider_key, &model), direction);
                rendered_lines = render_raw_model_selection(
                    fd,
                    rendered_lines,
                    &state,
                    &context.model_initial_model,
                    options.include_custom_model,
                    custom_focused,
                )?;
            }
        }
    }
}

#[cfg(unix)]
fn first_provider_key(context: &RawPromptActionContext) -> Option<String> {
    context
        .model_provider_groups
        .first()
        .map(|group| group.provider_key.clone())
}

#[cfg(unix)]
fn read_custom_model_selection(
    fd: RawFd,
    capture: &RawInputCapture,
    state: &ModelPickerState,
    provider_key: Option<String>,
) -> io::Result<Option<ModelSelection>> {
    let Some(provider_key) = provider_key else {
        return Ok(None);
    };
    let title = raw_auth_model_picker_title(state);
    let Some(model) =
        read_raw_auth_text_input(fd, capture, &title, &tr("Enter custom model name: "), "")?
    else {
        return Ok(None);
    };
    let model = model.trim();
    if model.is_empty() {
        return Ok(None);
    }
    Ok(Some(ModelSelection {
        provider_key,
        model: model.to_owned(),
        effort: None,
    }))
}

#[cfg(unix)]
fn render_raw_model_selection(
    fd: RawFd,
    previous_lines: usize,
    state: &ModelPickerState,
    current_model: &str,
    include_custom_model: bool,
    custom_focused: bool,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) = if include_custom_model {
        raw_model_picker_render_output(previous_lines, state, current_model, custom_focused, width)
    } else {
        raw_model_select_render_output(
            previous_lines,
            state,
            current_model,
            false,
            custom_focused,
            width,
        )
    };
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn raw_model_picker_render_output(
    previous_lines: usize,
    state: &ModelPickerState,
    current_model: &str,
    custom_focused: bool,
    width: usize,
) -> (String, usize) {
    raw_model_select_render_output(
        previous_lines,
        state,
        current_model,
        true,
        custom_focused,
        width,
    )
}

#[cfg(unix)]
pub(super) fn raw_model_select_render_output(
    previous_lines: usize,
    state: &ModelPickerState,
    current_model: &str,
    include_custom_model: bool,
    custom_focused: bool,
    width: usize,
) -> (String, usize) {
    let mut options = Vec::new();
    let mut focused = 0usize;
    let include_headers = state
        .items()
        .iter()
        .filter(|item| matches!(item, ModelPickerEntry::Header { .. }))
        .count()
        > 1;
    for item in state.items() {
        match item {
            ModelPickerEntry::Header { display_name } => {
                if include_headers {
                    options.push(RawSelectOption::disabled(display_name.clone()));
                }
            }
            ModelPickerEntry::Model {
                provider_key,
                model,
            } => {
                let mut label = model.to_owned();
                if model == current_model {
                    label.push_str(&tr(" (current)"));
                }
                if let Some(effort) = state.effort_for(provider_key, model) {
                    label.push(' ');
                    label.push_str(effort_level_symbol(effort));
                }
                if matches!(
                    state.items().get(state.focused_index()),
                    Some(ModelPickerEntry::Model {
                        provider_key: focused_provider,
                        model: focused_model,
                    }) if focused_provider == provider_key && focused_model == model
                ) {
                    focused = options.len();
                }
                options.push(RawSelectOption::new(label));
            }
        }
    }
    if include_custom_model {
        if custom_focused {
            focused = options.len();
        }
        options.push(RawSelectOption::new(tr("Custom model...")));
    }
    let focused = raw_select_initial_focus(&options, focused);
    raw_select_render_output(
        previous_lines,
        &raw_auth_model_picker_title(state),
        &options,
        focused,
        width,
    )
}
