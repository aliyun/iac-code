# Skill Renderer Shell Boundary Design

## Problem

`SkillRenderer` currently treats a skill as one string:

1. substitute skill arguments such as `$ARGUMENTS`, `$0`, and named arguments;
2. substitute built-in variables such as `${SKILL_DIR}`;
3. scan the resulting text for renderer shell syntax;
4. execute matching shell snippets.

That order lets user or model supplied skill arguments change what the renderer executes. It also makes skill argument syntax conflict with normal shell variable syntax inside shell snippets.

The vulnerable cases include:

- a normal text placeholder rendering into a new `````!` shell block;
- a placeholder inside an existing shell command rendering into additional shell syntax;
- block shell output containing inline shell syntax that is scanned again later.

## Goals

- Treat skill arguments as data, not executable renderer shell code.
- Execute only shell snippets that were present in the original skill file.
- Preserve existing renderer shell functionality for explicitly authored skill shell snippets.
- Keep permission detection and renderer execution detection aligned.
- Cover both model-invoked `SkillTool` use and direct skill command processing, since both share the renderer path.

## Non-Goals

- Removing renderer shell support entirely.
- Adding fine-grained user confirmation for each renderer shell command.
- Changing bundled skill trust rules.
- Reworking the broader tool permission pipeline.

## Design

The renderer will parse the original skill content into ordered segments before any argument substitution:

- text segments;
- inline shell segments matching ``!`command```;
- block shell segments matching `````! ... `````.

Only shell segments discovered in that original parse are executable. The renderer will not scan the fully rendered prompt for new shell snippets.

Text segments get normal skill argument substitution. If a text segment renders into ``!`...``` or `````! ... ``````, that output remains ordinary prompt text and is not executed.

Shell segments do not get skill argument substitution. Strings such as `$ARGUMENTS`, `$0`, `$1`, `$name`, `$PATH`, and `$HOME` remain part of the original shell command. This avoids treating shell variables as skill placeholders and prevents arguments from constructing commands.

Built-in variables such as `${SKILL_DIR}` and `${SESSION_ID}` are not user/model arguments. The implementation should keep their current replacement behavior while ensuring they do not cause newly rendered shell syntax to be scanned as executable code.

Shell execution output is appended as the replacement for the original shell segment. The output is never re-scanned for more shell snippets.

## Components

`src/iac_code/skills/renderer.py`

- Add a small segment model for text, inline shell, and block shell segments.
- Add a parser that uses the existing shell regexes to produce ordered, non-overlapping segments from the original content.
- Update `render_skill_prompt()` to parse original content first, render text segments, execute original shell segments, and join the results.
- Update or replace `execute_shell_commands()` so it executes shell segments from the original parse rather than repeatedly scanning the mutated prompt.
- Add a shared `contains_shell_commands(content: str) -> bool` helper based on the same parser or regex rules.

`src/iac_code/skills/skill_tool.py`

- Update `_has_only_safe_properties()` to use the shared renderer shell detection helper. Permission checks should classify a skill as shell-bearing using the same syntax that the renderer can execute.

Tests

- Extend `tests/skills/test_renderer.py` for segmentation and injection behavior.
- Extend `tests/skills/test_skill_tool.py` for shared shell detection.

## Data Flow

For a skill command:

1. Load original `skill.content`.
2. If needed, prefix the skill root text as a text segment.
3. Parse original skill content into text and shell segments.
4. Render text segments with skill arguments and built-in variables.
5. Render shell segments by executing the original shell command text without skill argument substitution.
6. Join rendered text and shell output.
7. Wrap the final prompt in `process_prompt_command()` as before.

## Error Handling

Argument-created shell syntax is not an error because it is no longer executable. It remains prompt text.

Shell execution failures continue to use existing behavior from `_run_shell()`, returning shell error markers or command output as appropriate.

Parser edge cases should favor text over execution. If syntax is malformed and does not match the renderer shell pattern, it remains text.

## Testing Plan

- Text placeholder renders into shell block syntax and does not call `_run_shell()`.
- Text placeholder renders into inline shell syntax and does not call `_run_shell()`.
- Shell block containing `$ARGUMENTS`, `$0`, `$1`, or `$name` executes the original command text without skill argument substitution.
- Original inline shell still executes and replaces the inline segment with trimmed output.
- Original block shell still executes and replaces the block segment with output.
- Block shell output containing inline shell syntax is not scanned or executed again.
- Multiple shell snippets and text snippets preserve order.
- `_has_only_safe_properties()` returns false for the same inline and block shell syntax that the renderer executes.

## Verification

Run focused tests first:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py tests/skills/test_skill_tool.py -v
```

Then run broader relevant tests:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills tests/services/permissions/test_pipeline.py -v
```

The current full-suite baseline has unrelated i18n failures because `src/iac_code/i18n/messages.pot` is missing. If full verification is run, report those baseline failures separately from this change.
