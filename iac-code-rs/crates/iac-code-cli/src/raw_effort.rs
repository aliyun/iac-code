use std::io;
use std::os::fd::RawFd;

use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{
    get_active_provider_key, load_active_provider_effort, load_saved_model,
};
use iac_code_tui::{EffortLevel, ModelThinkingSpec, RawInputCapture};

use super::cli_i18n::tr_value;
use super::raw_picker::{
    clear_raw_picker, raw_picker_terminal_width, write_raw_interactive_fd_all,
};
use super::raw_prompt_context::RawPromptActionContext;
use super::raw_select::{raw_select_render_output, RawSelectOption};
use super::raw_transcript::RawAlternateScreenGuard;

pub(super) fn read_raw_effort_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<EffortLevel>> {
    if context.effort_allowed.is_empty() {
        return Ok(None);
    }
    let _screen = RawAlternateScreenGuard::enter(fd)?;
    let mut focused = context
        .effort_current
        .and_then(|current| {
            context
                .effort_allowed
                .iter()
                .position(|effort| *effort == current)
        })
        .unwrap_or(0);
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_effort_picker(fd, rendered_lines, focused, context)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let selected = context.effort_allowed.get(focused).copied();
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            focused = focused.saturating_sub(1);
            rendered_lines = render_raw_effort_picker(fd, rendered_lines, focused, context)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            focused = (focused + 1).min(context.effort_allowed.len() - 1);
            rendered_lines = render_raw_effort_picker(fd, rendered_lines, focused, context)?;
        }
    }
}

fn render_raw_effort_picker(
    fd: RawFd,
    previous_lines: usize,
    focused: usize,
    context: &RawPromptActionContext,
) -> io::Result<usize> {
    let width = raw_picker_terminal_width(fd);
    let (output, line_count) =
        raw_effort_picker_render_output(previous_lines, focused, context, width);
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

pub(super) fn raw_effort_picker_render_output(
    previous_lines: usize,
    focused: usize,
    context: &RawPromptActionContext,
    width: usize,
) -> (String, usize) {
    let title = tr_value("Select effort for {model}", "model", &context.effort_model);
    let options = context
        .effort_allowed
        .iter()
        .map(|effort| {
            RawSelectOption::new(format!(
                "{} {}",
                effort_level_symbol(*effort),
                effort_level_label(*effort)
            ))
        })
        .collect::<Vec<_>>();
    raw_select_render_output(previous_lines, &title, &options, focused, width)
}

pub(super) fn effort_spec(
    provider_key: &str,
    model: &str,
) -> Option<(&'static [&'static str], &'static str)> {
    const OPENAI_EFFORTS: &[&str] = &["low", "medium", "high", "xhigh"];
    const GEMINI_EFFORTS: &[&str] = &["low", "medium", "high"];
    const ANTHROPIC_EFFORTS: &[&str] = &["low", "medium", "high", "xhigh", "max", "auto"];
    const DEEPSEEK_EFFORTS: &[&str] = &["high", "max"];

    match provider_key {
        "anthropic" | "anthropic_compatible"
            if matches!(
                model,
                "claude-opus-4-7"
                    | "claude-opus-4-6"
                    | "claude-sonnet-4-6"
                    | "claude-sonnet-4-6-1m"
                    | "claude-haiku-4-5-20251001"
            ) =>
        {
            Some((ANTHROPIC_EFFORTS, "high"))
        }
        "openai"
            if matches!(
                model,
                "gpt-5.5"
                    | "gpt-5.4"
                    | "gpt-5.4-mini"
                    | "gpt-5.3-codex"
                    | "gpt-5.2"
                    | "o3"
                    | "o4-mini"
            ) =>
        {
            Some((OPENAI_EFFORTS, "high"))
        }
        "deepseek" if matches!(model, "deepseek-v4-pro" | "deepseek-v4-flash") => {
            Some((DEEPSEEK_EFFORTS, "high"))
        }
        "dashscope" if matches!(model, "deepseek-v4-pro" | "deepseek-v4-flash") => {
            Some((DEEPSEEK_EFFORTS, "high"))
        }
        "gemini"
            if matches!(
                model,
                "gemini-3.5-flash"
                    | "gemini-3.1-pro-preview"
                    | "gemini-3-flash-preview"
                    | "gemini-3.1-flash-lite"
                    | "gemini-3.1-flash-lite-preview"
                    | "gemini-2.5-pro"
                    | "gemini-2.5-flash"
            ) =>
        {
            Some((GEMINI_EFFORTS, "medium"))
        }
        "aliyun_codingplan" | "aliyun_codingplan_intl"
            if matches!(model, "deepseek-v4-pro" | "deepseek-v4-flash") =>
        {
            Some((DEEPSEEK_EFFORTS, "high"))
        }
        _ => None,
    }
}

pub(super) fn raw_model_thinking_spec(provider_key: &str, model: &str) -> ModelThinkingSpec {
    let Some((allowed_efforts, default_effort)) = effort_spec(provider_key, model) else {
        return ModelThinkingSpec::none();
    };
    let allowed = allowed_efforts
        .iter()
        .filter_map(|effort| effort_level_from_label(effort))
        .collect::<Vec<_>>();
    if allowed.is_empty() {
        return ModelThinkingSpec::none();
    }
    ModelThinkingSpec::new(allowed, effort_level_from_label(default_effort))
}

pub(super) fn raw_effort_picker_context(
    paths: &ConfigPaths,
) -> (String, Vec<EffortLevel>, Option<EffortLevel>) {
    let Some(provider_key) = get_active_provider_key(paths).ok().flatten() else {
        return (String::new(), Vec::new(), None);
    };
    let Some(model) = load_saved_model(paths).ok().flatten() else {
        return (String::new(), Vec::new(), None);
    };
    let Some((allowed_labels, default_label)) = effort_spec(&provider_key, &model) else {
        return (model, Vec::new(), None);
    };
    let allowed = allowed_labels
        .iter()
        .filter_map(|label| effort_level_from_label(label))
        .collect::<Vec<_>>();
    if allowed.is_empty() {
        return (model, Vec::new(), None);
    }
    let default_effort = effort_level_from_label(default_label)
        .filter(|effort| allowed.contains(effort))
        .unwrap_or(allowed[0]);
    let current = load_active_provider_effort(paths)
        .ok()
        .flatten()
        .and_then(|label| effort_level_from_label(label.trim()))
        .filter(|effort| allowed.contains(effort))
        .unwrap_or(default_effort);
    (model, allowed, Some(current))
}

fn effort_level_from_label(value: &str) -> Option<EffortLevel> {
    match value {
        "low" => Some(EffortLevel::Low),
        "medium" => Some(EffortLevel::Medium),
        "high" => Some(EffortLevel::High),
        "xhigh" => Some(EffortLevel::XHigh),
        "max" => Some(EffortLevel::Max),
        "auto" => Some(EffortLevel::Auto),
        _ => None,
    }
}

pub(super) fn effort_level_label(value: EffortLevel) -> &'static str {
    match value {
        EffortLevel::Low => "low",
        EffortLevel::Medium => "medium",
        EffortLevel::High => "high",
        EffortLevel::XHigh => "xhigh",
        EffortLevel::Max => "max",
        EffortLevel::Auto => "auto",
    }
}

pub(super) fn effort_level_symbol(value: EffortLevel) -> &'static str {
    match value {
        EffortLevel::Low => "◆",
        EffortLevel::Medium => "◆◆",
        EffortLevel::High => "◆◆◆",
        EffortLevel::XHigh => "◆◆◆◆",
        EffortLevel::Max => "◆◆◆◆◆",
        EffortLevel::Auto => "◆",
    }
}
