use std::io;
use std::os::fd::RawFd;
use std::path::Path;

use iac_code_protocol::message::AgentMessageContent;
use iac_code_tui::{
    default_global_keybinding_manager, InputHistory, PromptEditOutcome, PromptEditor,
    RawInputCapture, SkillCatalog, SuggestionProvider,
};

use crate::raw_picker::raw_picker_insertable_text;
use crate::raw_prompt_context::RawPromptActionContext;
#[cfg(test)]
use crate::raw_prompt_images::EmptyRawPromptImageSource;
use crate::raw_prompt_images::{raw_prompt_content_from_pasted_images, SystemRawPromptImageSource};
pub(super) use crate::raw_prompt_images::{
    raw_prompt_image_refs, raw_prompt_persist_pasted_image, RawPromptImageSource,
    RawPromptPastedImage,
};
use crate::raw_prompt_renderer::{
    clear_raw_interactive_prompt_current, raw_prompt_suggestion_overlay,
    render_raw_interactive_prompt_with_clipboard_hint,
    render_raw_interactive_prompt_with_overlay_and_clipboard_hint,
    write_raw_interactive_prompt_newline, RawPromptRenderParams, RawPromptRenderState,
};
use crate::raw_prompt_submit::{
    handle_raw_prompt_submit, RawPromptSubmitContext, RawPromptSubmitOutcome,
};
use crate::raw_search::{read_raw_global_search, read_raw_history_search, read_raw_quick_open};
use crate::raw_suggestions::raw_interactive_suggestion_providers;
use crate::raw_transcript::read_raw_transcript_view;

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq)]
pub(super) struct RawInteractivePromptInput {
    pub(super) text: String,
    pub(super) prompt_content: Option<AgentMessageContent>,
    pub(super) prehandled: bool,
    pub(super) transcript_lines: Vec<String>,
}

#[cfg(unix)]
impl RawInteractivePromptInput {
    pub(super) fn from_text_and_pasted_images(
        text: String,
        images: &[RawPromptPastedImage],
    ) -> Self {
        Self {
            prompt_content: raw_prompt_content_from_pasted_images(&text, images),
            text,
            prehandled: false,
            transcript_lines: Vec::new(),
        }
    }

    pub(super) fn prehandled(text: String, transcript_lines: Vec<String>) -> Self {
        Self {
            text,
            prompt_content: None,
            prehandled: true,
            transcript_lines,
        }
    }
}

#[cfg(all(unix, test))]
pub(super) fn read_raw_interactive_prompt(
    fd: RawFd,
    input_history: Option<&mut InputHistory>,
    suggestion_root: &Path,
    skill_catalog: SkillCatalog,
) -> io::Result<Option<String>> {
    Ok(
        read_raw_interactive_prompt_input(fd, input_history, suggestion_root, skill_catalog)?
            .map(|input| input.text),
    )
}

#[cfg(all(unix, test))]
pub(super) fn read_raw_interactive_prompt_input(
    fd: RawFd,
    input_history: Option<&mut InputHistory>,
    suggestion_root: &Path,
    skill_catalog: SkillCatalog,
) -> io::Result<Option<RawInteractivePromptInput>> {
    let mut image_source = EmptyRawPromptImageSource;
    let context = RawPromptActionContext::default();
    read_raw_interactive_prompt_input_with_context_and_image_source(
        fd,
        input_history,
        suggestion_root,
        raw_interactive_suggestion_providers(suggestion_root, skill_catalog),
        &context,
        &mut image_source,
        &mut Vec::new(),
    )
}

#[cfg(unix)]
pub(super) fn read_raw_interactive_prompt_input_with_context(
    fd: RawFd,
    input_history: Option<&mut InputHistory>,
    suggestion_root: &Path,
    skill_catalog: SkillCatalog,
    context: &RawPromptActionContext,
    pasted_images: &mut Vec<RawPromptPastedImage>,
) -> io::Result<Option<RawInteractivePromptInput>> {
    let mut image_source = SystemRawPromptImageSource;
    read_raw_interactive_prompt_input_with_context_and_image_source(
        fd,
        input_history,
        suggestion_root,
        raw_interactive_suggestion_providers(suggestion_root, skill_catalog),
        context,
        &mut image_source,
        pasted_images,
    )
}

#[cfg(all(unix, test))]
pub(super) fn read_raw_interactive_prompt_with_providers(
    fd: RawFd,
    input_history: Option<&mut InputHistory>,
    action_root: &Path,
    suggestion_providers: Vec<Box<dyn SuggestionProvider>>,
) -> io::Result<Option<String>> {
    let mut image_source = EmptyRawPromptImageSource;
    let context = RawPromptActionContext::default();
    Ok(
        read_raw_interactive_prompt_input_with_context_and_image_source(
            fd,
            input_history,
            action_root,
            suggestion_providers,
            &context,
            &mut image_source,
            &mut Vec::new(),
        )?
        .map(|input| input.text),
    )
}

#[cfg(unix)]
pub(super) fn read_raw_interactive_prompt_input_with_context_and_image_source(
    fd: RawFd,
    input_history: Option<&mut InputHistory>,
    action_root: &Path,
    suggestion_providers: Vec<Box<dyn SuggestionProvider>>,
    context: &RawPromptActionContext,
    image_source: &mut dyn RawPromptImageSource,
    pasted_images: &mut Vec<RawPromptPastedImage>,
) -> io::Result<Option<RawInteractivePromptInput>> {
    let capture = RawInputCapture::enter(fd)?;
    let bindings = default_global_keybinding_manager();
    let mut editor = PromptEditor::new(suggestion_providers);
    let mut input_history = input_history;
    // `pasted_images` is owned by the caller and persists across prompts within
    // the session, so a recalled `[Image #N]` keeps its clickable link and is
    // re-attached on submit. Ids stay monotonic because the vec only grows.
    let mut clipboard_has_image = image_source.has_image()?;
    let mut rendered_prompt_state = RawPromptRenderState::empty();
    rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
        fd,
        rendered_prompt_state,
        editor.text(),
        editor.cursor(),
        &editor.ghost_text(),
        pasted_images,
        clipboard_has_image,
    )?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            return Ok(None);
        };
        if event.key == "focus_in" {
            clipboard_has_image = image_source.has_image()?;
            rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                fd,
                rendered_prompt_state,
                editor.text(),
                editor.cursor(),
                &editor.ghost_text(),
                pasted_images,
                clipboard_has_image,
            )?;
            continue;
        }
        if event.key == "paste" || raw_picker_insertable_text(&event) {
            clipboard_has_image = false;
        }
        let outcome = if let Some(history) = input_history.as_mut() {
            editor.handle_key_with_history_and_bindings(event, history, &bindings)
        } else {
            editor.handle_key_with_bindings(event, &bindings)
        };
        match outcome {
            PromptEditOutcome::Submit(prompt) => {
                match handle_raw_prompt_submit(
                    prompt,
                    &mut editor,
                    rendered_prompt_state,
                    RawPromptSubmitContext {
                        fd,
                        capture: &capture,
                        context,
                        pasted_images,
                        clipboard_has_image,
                    },
                )? {
                    RawPromptSubmitOutcome::Continue(next_state) => {
                        rendered_prompt_state = next_state;
                        continue;
                    }
                    RawPromptSubmitOutcome::Return(input) => return Ok(Some(input)),
                }
            }
            PromptEditOutcome::Cancel => {
                write_raw_interactive_prompt_newline(fd)?;
                return Ok(None);
            }
            PromptEditOutcome::Action(action) if action == "open_history_search" => {
                clear_raw_interactive_prompt_current(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    &editor.ghost_text(),
                )?;
                rendered_prompt_state = RawPromptRenderState::empty();
                if let Some(selected) =
                    read_raw_history_search(fd, &capture, input_history.as_deref())?
                {
                    editor.insert_text(&selected);
                }
                rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    editor.cursor(),
                    &editor.ghost_text(),
                    pasted_images,
                    clipboard_has_image,
                )?;
            }
            PromptEditOutcome::Action(action) if action == "open_quick_open" => {
                clear_raw_interactive_prompt_current(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    &editor.ghost_text(),
                )?;
                rendered_prompt_state = RawPromptRenderState::empty();
                if let Some(selected) = read_raw_quick_open(fd, &capture, action_root)? {
                    editor.insert_text(&selected);
                }
                rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    editor.cursor(),
                    &editor.ghost_text(),
                    pasted_images,
                    clipboard_has_image,
                )?;
            }
            PromptEditOutcome::Action(action) if action == "open_global_search" => {
                clear_raw_interactive_prompt_current(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    &editor.ghost_text(),
                )?;
                rendered_prompt_state = RawPromptRenderState::empty();
                if let Some(selected) = read_raw_global_search(fd, &capture, action_root)? {
                    editor.insert_text(&selected);
                }
                rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    editor.cursor(),
                    &editor.ghost_text(),
                    pasted_images,
                    clipboard_has_image,
                )?;
            }
            PromptEditOutcome::Action(action) if action == "paste_image" => {
                if let Some(mut image) = image_source.read_image()? {
                    clipboard_has_image = false;
                    let image_id = pasted_images.len() + 1;
                    raw_prompt_persist_pasted_image(&mut image, image_id, context);
                    pasted_images.push(image);
                    let placeholder = format!("[Image #{image_id}]");
                    editor.insert_text(&placeholder);
                }
                rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    editor.cursor(),
                    &editor.ghost_text(),
                    pasted_images,
                    clipboard_has_image,
                )?;
            }
            PromptEditOutcome::Action(action) if action == "expand_last_turn" => {
                clear_raw_interactive_prompt_current(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    &editor.ghost_text(),
                )?;
                rendered_prompt_state = RawPromptRenderState::empty();
                read_raw_transcript_view(fd, &capture, &context.transcript_lines)?;
                rendered_prompt_state = render_raw_interactive_prompt_with_clipboard_hint(
                    fd,
                    rendered_prompt_state,
                    editor.text(),
                    editor.cursor(),
                    &editor.ghost_text(),
                    pasted_images,
                    clipboard_has_image,
                )?;
            }
            PromptEditOutcome::Continue | PromptEditOutcome::Action(_) => {
                rendered_prompt_state =
                    render_raw_interactive_prompt_with_overlay_and_clipboard_hint(
                        fd,
                        RawPromptRenderParams {
                            previous_state: rendered_prompt_state,
                            text: editor.text(),
                            cursor: editor.cursor(),
                            ghost_text: &editor.ghost_text(),
                            suggestions: raw_prompt_suggestion_overlay(&editor),
                            images: pasted_images,
                            clipboard_has_image,
                        },
                    )?;
            }
        }
    }
}
