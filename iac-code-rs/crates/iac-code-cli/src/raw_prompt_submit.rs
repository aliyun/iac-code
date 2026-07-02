use std::io;
use std::os::fd::RawFd;

use iac_code_tui::{PromptEditor, RawInputCapture};

use crate::cli_i18n::tr;
use crate::interactive_commands::format_interactive_command_result;
use crate::raw_auth::read_raw_auth_flow;
use crate::raw_effort::{effort_level_label, read_raw_effort_picker};
use crate::raw_memory::{raw_memory_action_message, read_raw_memory_dialog};
use crate::raw_model_effort::read_raw_model_picker;
use crate::raw_picker::write_raw_interactive_fd_all;
use crate::raw_prompt_commands::{
    raw_prompt_should_open_auth_flow, raw_prompt_should_open_effort_picker,
    raw_prompt_should_open_memory_dialog, raw_prompt_should_open_model_picker,
    raw_prompt_should_open_rename_prompt, raw_prompt_should_open_resume_picker,
    raw_prompt_should_open_skills_picker,
};
use crate::raw_prompt_context::RawPromptActionContext;
use crate::raw_prompt_input::{RawInteractivePromptInput, RawPromptPastedImage};
use crate::raw_prompt_renderer::{
    clear_raw_interactive_prompt_current, render_raw_interactive_prompt_with_clipboard_hint,
    write_raw_interactive_prompt_newline, write_raw_interactive_prompt_submit_newline,
    RawPromptRenderState,
};
use crate::raw_rename::read_raw_rename_name_prompt;
use crate::raw_resume::read_raw_resume_picker;
use crate::raw_skills::{read_raw_skills_picker, RawSkillsPickerOutcome};

pub(super) enum RawPromptSubmitOutcome {
    Continue(RawPromptRenderState),
    Return(RawInteractivePromptInput),
}

pub(super) struct RawPromptSubmitContext<'a> {
    pub(super) fd: RawFd,
    pub(super) capture: &'a RawInputCapture,
    pub(super) context: &'a RawPromptActionContext,
    pub(super) pasted_images: &'a [RawPromptPastedImage],
    pub(super) clipboard_has_image: bool,
}

pub(super) fn handle_raw_prompt_submit(
    prompt: String,
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    if raw_prompt_should_open_resume_picker(&prompt)
        && !submit.context.resume_current_project_entries.is_empty()
    {
        return handle_raw_prompt_resume_submit(prompt, editor, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_rename_prompt(&prompt) {
        return handle_raw_prompt_rename_submit(prompt, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_auth_flow(&prompt) && submit.context.config_paths.is_some() {
        return handle_raw_prompt_auth_submit(editor, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_model_picker(&prompt)
        && !submit.context.model_provider_groups.is_empty()
    {
        return handle_raw_prompt_model_submit(editor, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_effort_picker(&prompt) && !submit.context.effort_allowed.is_empty() {
        return handle_raw_prompt_effort_submit(editor, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_memory_dialog(&prompt) && submit.context.config_paths.is_some() {
        return handle_raw_prompt_memory_submit(editor, rendered_prompt_state, submit);
    }
    if raw_prompt_should_open_skills_picker(&prompt)
        && !submit.context.skill_management_items.is_empty()
    {
        return handle_raw_prompt_skills_submit(prompt, rendered_prompt_state, submit);
    }

    write_raw_interactive_prompt_submit_newline(
        submit.fd,
        rendered_prompt_state,
        &prompt,
        submit.pasted_images,
    )?;
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::from_text_and_pasted_images(prompt, submit.pasted_images),
    ))
}

fn handle_raw_prompt_resume_submit(
    prompt: String,
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    write_raw_interactive_prompt_submit_newline(
        submit.fd,
        rendered_prompt_state,
        &prompt,
        submit.pasted_images,
    )?;
    if let Some(session_id) = read_raw_resume_picker(submit.fd, submit.capture, submit.context)? {
        let selected_prompt = format!("/resume {session_id}");
        editor.set_text(selected_prompt.clone());
        render_raw_interactive_prompt_with_clipboard_hint(
            submit.fd,
            RawPromptRenderState::empty(),
            editor.text(),
            editor.cursor(),
            &editor.ghost_text(),
            submit.pasted_images,
            submit.clipboard_has_image,
        )?;
        write_raw_interactive_prompt_newline(submit.fd)?;
        return Ok(RawPromptSubmitOutcome::Return(
            RawInteractivePromptInput::from_text_and_pasted_images(
                selected_prompt,
                submit.pasted_images,
            ),
        ));
    }

    let selected_prompt = "/resume".to_owned();
    let resume_cancelled = tr("Resume cancelled");
    write_raw_dim_command_message(submit.fd, &resume_cancelled)?;
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::prehandled(
            selected_prompt.clone(),
            vec![
                format!("❯ {selected_prompt}"),
                format_interactive_command_result(&resume_cancelled),
            ],
        ),
    ))
}

fn handle_raw_prompt_rename_submit(
    prompt: String,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    write_raw_interactive_prompt_submit_newline(
        submit.fd,
        rendered_prompt_state,
        &prompt,
        submit.pasted_images,
    )?;
    let Some(name) = read_raw_rename_name_prompt(submit.fd, submit.capture)? else {
        let rename_cancelled = tr("Rename cancelled");
        write_raw_dim_command_message(submit.fd, &rename_cancelled)?;
        return Ok(RawPromptSubmitOutcome::Return(
            RawInteractivePromptInput::prehandled(
                "/rename".to_owned(),
                vec![
                    "❯ /rename".to_owned(),
                    format_interactive_command_result(&rename_cancelled),
                ],
            ),
        ));
    };
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::from_text_and_pasted_images(
            format!("/rename {name}"),
            submit.pasted_images,
        ),
    ))
}

fn handle_raw_prompt_auth_submit(
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    clear_raw_prompt_for_picker(submit.fd, rendered_prompt_state, editor)?;
    if let Some(auth_message) = read_raw_auth_flow(submit.fd, submit.capture, submit.context)? {
        return render_prehandled_command_message("/auth", &auth_message, editor, submit, false);
    }
    render_prehandled_command_message("/auth", &tr("Auth cancelled"), editor, submit, true)
}

fn handle_raw_prompt_model_submit(
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    clear_raw_prompt_for_picker(submit.fd, rendered_prompt_state, editor)?;
    if let Some(selection) = read_raw_model_picker(submit.fd, submit.capture, submit.context)? {
        let selected_prompt = match selection.effort {
            Some(effort) => format!("/model {} {}", selection.model, effort_level_label(effort)),
            None => format!("/model {}", selection.model),
        };
        render_selected_prompt(editor, selected_prompt, submit)
    } else {
        rerender_after_picker_cancel(editor, submit)
    }
}

fn handle_raw_prompt_effort_submit(
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    clear_raw_prompt_for_picker(submit.fd, rendered_prompt_state, editor)?;
    if let Some(effort) = read_raw_effort_picker(submit.fd, submit.capture, submit.context)? {
        let selected_prompt = format!("/effort {}", effort_level_label(effort));
        render_selected_prompt(editor, selected_prompt, submit)
    } else {
        rerender_after_picker_cancel(editor, submit)
    }
}

fn handle_raw_prompt_memory_submit(
    editor: &mut PromptEditor,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    clear_raw_prompt_for_picker(submit.fd, rendered_prompt_state, editor)?;
    if let Some(action) = read_raw_memory_dialog(submit.fd, submit.capture, submit.context)? {
        let message = raw_memory_action_message(&action);
        render_prehandled_command_message("/memory", &message, editor, submit, false)
    } else {
        rerender_after_picker_cancel(editor, submit)
    }
}

fn handle_raw_prompt_skills_submit(
    prompt: String,
    rendered_prompt_state: RawPromptRenderState,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    write_raw_interactive_prompt_submit_newline(
        submit.fd,
        rendered_prompt_state,
        &prompt,
        submit.pasted_images,
    )?;
    let outcome = read_raw_skills_picker(submit.fd, submit.capture, submit.context)?;
    let selected_prompt = "/skills".to_owned();
    let message = match outcome {
        RawSkillsPickerOutcome::Saved(_) => tr("Skills updated"),
        RawSkillsPickerOutcome::Cancelled => tr("Skills update cancelled"),
    };
    write_raw_command_message(submit.fd, &message)?;
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::prehandled(
            selected_prompt.clone(),
            vec![
                format!("❯ {selected_prompt}"),
                format_interactive_command_result(&message),
            ],
        ),
    ))
}

fn clear_raw_prompt_for_picker(
    fd: RawFd,
    rendered_prompt_state: RawPromptRenderState,
    editor: &PromptEditor,
) -> io::Result<()> {
    clear_raw_interactive_prompt_current(
        fd,
        rendered_prompt_state,
        editor.text(),
        &editor.ghost_text(),
    )
}

fn render_selected_prompt(
    editor: &mut PromptEditor,
    selected_prompt: String,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    editor.set_text(selected_prompt.clone());
    render_raw_interactive_prompt_with_clipboard_hint(
        submit.fd,
        RawPromptRenderState::empty(),
        editor.text(),
        editor.cursor(),
        &editor.ghost_text(),
        submit.pasted_images,
        submit.clipboard_has_image,
    )?;
    write_raw_interactive_prompt_newline(submit.fd)?;
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::from_text_and_pasted_images(
            selected_prompt,
            submit.pasted_images,
        ),
    ))
}

fn render_prehandled_command_message(
    selected_prompt: &str,
    message: &str,
    editor: &mut PromptEditor,
    submit: RawPromptSubmitContext<'_>,
    dim: bool,
) -> io::Result<RawPromptSubmitOutcome> {
    editor.set_text(selected_prompt.to_owned());
    render_raw_interactive_prompt_with_clipboard_hint(
        submit.fd,
        RawPromptRenderState::empty(),
        editor.text(),
        editor.cursor(),
        &editor.ghost_text(),
        submit.pasted_images,
        submit.clipboard_has_image,
    )?;
    write_raw_interactive_prompt_newline(submit.fd)?;
    if dim {
        write_raw_dim_command_message(submit.fd, message)?;
    } else {
        write_raw_command_message(submit.fd, message)?;
    }
    Ok(RawPromptSubmitOutcome::Return(
        RawInteractivePromptInput::prehandled(
            selected_prompt.to_owned(),
            vec![
                format!("❯ {selected_prompt}"),
                format_interactive_command_result(message),
            ],
        ),
    ))
}

fn rerender_after_picker_cancel(
    editor: &PromptEditor,
    submit: RawPromptSubmitContext<'_>,
) -> io::Result<RawPromptSubmitOutcome> {
    render_raw_interactive_prompt_with_clipboard_hint(
        submit.fd,
        RawPromptRenderState::empty(),
        editor.text(),
        editor.cursor(),
        &editor.ghost_text(),
        submit.pasted_images,
        submit.clipboard_has_image,
    )
    .map(RawPromptSubmitOutcome::Continue)
}

fn write_raw_command_message(fd: RawFd, message: &str) -> io::Result<()> {
    write_raw_interactive_fd_all(
        fd,
        format!("{}\r\n", format_interactive_command_result(message)).as_bytes(),
    )
}

fn write_raw_dim_command_message(fd: RawFd, message: &str) -> io::Result<()> {
    write_raw_interactive_fd_all(
        fd,
        format!(
            "\x1b[2m{}\x1b[0m\r\n",
            format_interactive_command_result(message)
        )
        .as_bytes(),
    )
}
