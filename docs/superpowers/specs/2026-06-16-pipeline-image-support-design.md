# Pipeline Image Support Design

## Goal

Pipeline mode should support the same image input capability that normal chat mode supports. Images must work across REPL input, A2A input, running-pipeline interrupts, user-question resumes, candidate selection resumes, and process/session recovery.

This design intentionally scopes multimodal support to image input because normal mode currently supports image blocks, not arbitrary audio or binary payloads.

## Confirmed Decisions

- Support image input everywhere pipeline accepts user input.
- Allow pure-image input with no text.
- Reuse normal-mode image behavior wherever possible.
- Keep early model capability checks. If the current provider/model does not support images, fail or warn before starting work.
- Store image bytes inline as base64 in `ImageBlock.data`, matching normal-mode message storage.
- Resize/downsample A2A images using the same image processing path used by REPL images.
- Do not carry pipeline images into normal-mode handoff after pipeline completion. Handoff remains text summary only.
- Let interrupt judge see images. It may summarize image-derived information in `reason` or `rollback_context`.
- The final target step or candidate must still receive the original image blocks, not only the judge summary.

## Architecture

Add a small internal input wrapper, named `PipelineUserInput`, for all pipeline user input boundaries.

```python
@dataclass(frozen=True)
class PipelineUserInput:
    content: str | list[ContentBlock]
    display_text: str
    has_images: bool
```

`content` is the source of truth passed into `AgentLoop`. It can be a plain string or a structured list of `TextBlock` and `ImageBlock` values. `display_text` is used for UI rendering, A2A status events, logs, sidecar text fields, and interrupt prompt text. `has_images` lets callers treat pure-image input as non-empty.

This wrapper should not replace `Message` and should not change provider APIs. It only prevents REPL, A2A, pipeline runner, sidecar, and interrupt code from each inventing its own `str | list[ContentBlock]` handling.

## REPL Input Flow

Pipeline mode should stop dropping `PromptInputResult.pasted_contents`.

When REPL receives a pipeline input:

1. If the input is already a plain string, create `PipelineUserInput(content=text, display_text=text, has_images=False)`.
2. If the input is a `PromptInputResult`, call the existing `process_user_input(text, pasted_contents=...)`.
3. If any resulting block is `ImageBlock`, keep the structured block list as `content`.
4. If there are no image blocks, keep the plain text as `content`.
5. Compute `display_text` from the user-visible prompt text. For pure-image input, use a safe placeholder such as `[Image input]`.

The current pipeline warning saying images are ignored should be removed or inverted into tests that prove images are forwarded.

The existing image attach path already performs capability gating through `is_model_multimodal(...)`. Pipeline should reuse that behavior and should not accept pasted images that normal mode would reject.

## A2A Input Flow

A2A currently converts image-like parts into a text manifest. Pipeline image support needs a new conversion path that returns internal content blocks.

The converter should preserve existing text handling:

- Text parts become `TextBlock`.
- JSON data parts with `application/json` continue to serialize to compact text.
- Raw text and text file URLs continue to become text.

For supported image media types:

- `raw` image bytes decode directly from the part.
- `data` image parts read base64 bytes from fields such as `bytes` or `base64`.
- `file://` image URL parts read bytes from a safe workspace-local path, preserving existing workspace and symlink escape checks.
- Image bytes are passed through the shared resize/downsample helper.
- The resized bytes are base64 encoded and emitted as `ImageBlock(media_type=..., data=...)`.

Pipeline mode should treat image-only A2A requests as valid input. It should no longer fail them with a text-only message. If a request includes image input and the selected model is not multimodal, A2A should return a clear failed status rather than silently degrading to text.

Audio and `application/octet-stream` are not included in this feature. They can keep existing manifest behavior or be rejected in the new pipeline multimodal converter, but they must not become image blocks.

## Pipeline Runner Flow

`PipelineRunner.run`, `resume`, `continue_from_sidecar`, `handle_user_interrupt`, and related A2A bridge calls should accept either `str` or `PipelineUserInput`. Internally they normalize to `PipelineUserInput`.

The first step, resumed step, or injected target AgentLoop receives `PipelineUserInput.content`.

Pipeline status events and observability continue to use text-safe fields derived from `display_text`. Input length metrics should use `len(display_text)` and may add a boolean such as `has_images` if useful. Telemetry content capture should continue recording text only, never base64 image data.

`StepExecutor` already accepts `str | list[ContentBlock]`, so most step execution can remain unchanged. Helper functions that need text, such as completion guards or prompt context snapshots, should use text extracted from blocks or `display_text`.

## Persistence and Recovery

Pipeline step transcripts are the source of truth for recovering LLM context. They already store `Message.to_dict()` in JSONL and load through `Message.from_dict()`, so they can round-trip `ImageBlock` content.

Use these persistence rules:

- Pipeline transcripts store full structured `Message(content=list[ContentBlock])`, including image base64.
- Root visible session history stores only `display_text` for pipeline-visible user turns.
- Sidecar state machine `current_step_user_input` remains text-only `display_text`. It is a readable recovery hint, not the source of multimodal content.
- Session recovery should load repaired pipeline transcripts and preserve image blocks.
- Cache cleanup must not affect recovery because the transcript contains inline base64 image data.

This keeps sidecar metadata small while preserving full image context where it matters.

## Interrupt Judge

`InterruptController.judge` should accept normalized pipeline input and send the judge model both the routing prompt text and any image blocks.

For text plus image input, the judge request should contain:

- A `TextBlock` with the current pipeline state, routing instructions, and user display text.
- The original `ImageBlock` values from the user input.

For pure-image input, the text block should explicitly state that the user provided image input and that the judge should inspect it to determine routing.

`InterruptVerdict` remains text-oriented. The judge can put image-derived details into `reason` or `rollback_context`, for example: "The uploaded diagram shows ECS behind SLB connected to RDS; rollback to architecture planning."

Supplement behavior:

- The target parent step or candidate AgentLoop receives the original `PipelineUserInput.content`.
- Judge image-derived text is used for routing only and is not injected as an extra replacement message.

Hard interrupt behavior:

- The rollback target receives both judge `rollback_context` and the original image input.
- This can be represented by prepending a `TextBlock` containing `rollback_context` to the original content blocks.
- The original image blocks must be preserved so the target step can independently inspect the image.

If the judge fails or times out, existing interrupt failure policy still applies. The implementation must not silently drop image input.

## Error Handling

REPL:

- If image paste is unsupported by the current model, reuse the normal-mode warning and do not attach the image.
- If resize/downsample fails, reuse normal-mode image error handling.
- If pure-image input is submitted after a successful attach, it is valid.

A2A:

- Invalid base64, unsafe file URL, non-file path, symlink escape, oversized image, and unsupported image media type return sanitized failure statuses.
- Error messages must not leak local file paths or base64 content.
- Model-not-multimodal with image input returns a clear failed status.

Pipeline:

- No pipeline branch should convert image input into a text-only manifest when the target path expects true image support.
- Empty text plus images is valid. Empty text with no images is still invalid where it is invalid today.

## Testing Plan

REPL tests:

- Pipeline mode no longer warns that images are ignored.
- `PromptInputResult` with an image calls the pipeline handler with structured content.
- Pure-image input is accepted.
- Non-multimodal model still fails before attach, matching normal mode.

A2A part conversion tests:

- Raw image part becomes `ImageBlock`.
- Base64 data image part becomes `ImageBlock`.
- Safe file URL image part becomes `ImageBlock`.
- Resize/downsample is invoked for A2A images.
- Unsafe file URL, invalid base64, oversized content, and unsupported image media type fail safely.

A2A executor tests:

- Pipeline mode passes structured `PipelineUserInput` to the pipeline executor.
- Image-only requests are valid.
- Image input with non-multimodal model returns a clear failure.

Pipeline runner tests:

- `run`, `resume`, `continue_from_sidecar`, and `handle_user_interrupt` accept `PipelineUserInput`.
- Sidecar stores display text while transcripts store full image blocks.
- Restored transcripts preserve image blocks.
- Pure-image input is not treated as empty.

Interrupt tests:

- Judge provider request includes image blocks.
- Judge can produce image-derived `rollback_context`.
- Supplement injects original image input into the target AgentLoop.
- Hard interrupt restarts the target with both rollback context text and original image blocks.

Regression tests:

- Existing text-only pipeline behavior remains unchanged.
- Existing normal-mode image tests continue to pass.
- Existing A2A text, JSON, and text-file part handling remains unchanged.

## Verification

Relevant focused test commands:

```bash
uv run pytest tests/ui/test_repl_pipeline_image_warning.py tests/utils/image/test_processor.py tests/providers/test_openai_image_blocks.py
uv run pytest tests/a2a/test_parts.py tests/a2a/test_executor.py tests/a2a/test_pipeline_executor.py
uv run pytest tests/pipeline/engine/test_pipeline_runner.py tests/pipeline/engine/test_pipeline_runner_interrupt.py tests/pipeline/engine/test_pipeline_runner_sidecar_path.py
uv run pytest tests/pipeline/engine/test_interrupt.py tests/pipeline/engine/test_transcript_storage.py
```

After implementation, run `make test` if feasible. The current baseline is known to fail six i18n tests because `src/iac_code/i18n/messages.pot` is missing in this worktree; that baseline issue is independent of this image-support design.
