# UI Status, History, and i18n Reliability Design

## Summary

Fix GitHub issues #92, #84, #85, and #76 as one focused reliability pass across the interactive UI, status reporting, shell-history suggestions, and i18n plural handling.

The selected scope is approach 1: one coordinated fix set with small local changes. For issue #76, use the medium repair level: include tool definitions in context estimates and add model-family fallback estimation for non-OpenAI models, without adding real tokenizer SDK dependencies.

## Issues Covered

- #92: `ShellHistoryProvider` reads and parses the whole shell history file on every keystroke.
- #84:
  - `/model` telemetry reads `activeProvider` as a dict even though it is stored as a string.
  - `/status` compact token formatting loses precision for thousands.
  - Shell history re-read issue, overlapping #92.
- #85: resume picker plural strings are built with hardcoded English `s` suffixes.
- #76: `/status` context estimate excludes tool definitions and uses weak fallback token estimates for non-OpenAI model families.

## Goals

- Keep implementation changes local to existing module boundaries.
- Avoid new runtime dependencies for tokenization.
- Make `/status` context usage closer to provider input usage by counting tool schemas.
- Keep API-reported usage distinct from local context estimates.
- Preserve existing REPL behavior while reducing per-keystroke work.
- Make pluralized UI strings extractable and translatable through gettext catalogs.
- Restore the i18n test baseline by versioning the required `messages.pot` artifact.

## Non-Goals

- Do not introduce provider-specific real tokenizers.
- Do not redesign the suggestion aggregator or prompt input loop.
- Do not change provider wire formats.
- Do not change API usage accounting from provider responses.
- Do not refactor unrelated i18n strings.

## Architecture

### Shell History Suggestions

`ShellHistoryProvider` remains the owner of shell history loading. It will maintain:

- a cache key based on `(path, mtime_ns, size)`;
- cached parsed entries in oldest-first order;
- a maximum number of returned suggestions.

`provide()` will stat the history file, reuse cached entries when the key has not changed, and only call `_read_history()` on first use or after the file changes. Filtering, deduplication, and scoring stay inside `provide()`.

### Model Command Telemetry

`model_command()` will read the active provider key from `settings["activeProvider"]`, then read `apiBase` from `settings["providers"][active_key]["apiBase"]` when both structures are valid. This logic should be shared by explicit-argument and interactive model switching paths to avoid duplicate bugs.

### Status Formatting

`_format_compact()` will keep exact integers below 1,000 and million formatting as today. Thousands will use rounded decimal-aware formatting instead of integer floor division so values like 1,500 no longer display as `1k`.

### Plural i18n

`iac_code.i18n` will expose a stable `ngettext(singular, plural, n)` wrapper, mirroring the existing stable `_()` wrapper. `setup_i18n()` will update both gettext and ngettext delegates.

Resume picker strings for lines, messages, minutes, hours, and days will use `ngettext()`:

- `{n} more line` / `{n} more lines`
- `{n} message` / `{n} messages`
- `{n} minute ago` / `{n} minutes ago`
- `{n} hour ago` / `{n} hours ago`
- `{n} day ago` / `{n} days ago`

The source strings will use named placeholders and `str.format()`.

### Context and Tool Definition Tokens

`TokenCounter` will gain a tool-definition counting method. It will count the text portions that providers send for tool schemas: tool name, description, and serialized JSON schema. The exact provider envelope differs, so this remains an estimate, but it will include the missing schema payload.

`ContextManager` will cache tool definition tokens separately from system and message tokens. The total used by `get_total_tokens()`, `get_usage()`, and `needs_compaction()` will include:

- system prompt tokens;
- user message tokens;
- assistant message tokens;
- tool result tokens;
- tool definition tokens.

`AgentLoop` will synchronize current tool definitions to the context manager after `_get_tool_definitions()` is built and before compaction checks and provider calls. This ensures `/status` and automatic compaction use the same estimate for the next request.

### Non-OpenAI Token Fallback

`TokenCounter` will retain tiktoken for OpenAI-family encodings. For non-OpenAI model families such as Qwen, Kimi, GLM, Doubao, MiniMax, and Gemini, it will use a model-family fallback strategy instead of treating `cl100k_base` as universally accurate.

The fallback strategy will estimate mixed-language content by counting CJK characters more conservatively than ASCII text. This improves Chinese-heavy estimates without adding tokenizer SDKs. The estimates remain approximate and should be documented as local context estimates, not billing-grade token counts.

## Data Flow

1. REPL creates the tool registry and agent loop as today.
2. `AgentLoop._run_streaming_inner()` builds provider tool definitions.
3. The agent loop updates `ContextManager` with those tool definitions.
4. `ContextManager.needs_compaction()` includes tool definition tokens.
5. Provider requests continue to receive the same `tools` payload as before.
6. `/status` calls `InlineREPL.get_status_snapshot()`, which reads `AgentLoop.get_context_usage()`.
7. The status panel displays API usage from provider-reported session totals and context usage from local estimates.

## Error Handling

- Shell history stat/read failures return an empty suggestion list and do not affect typing.
- Cache refresh failures keep behavior safe by clearing or ignoring the stale cache.
- Tool definition counting failures fall back to zero for that update rather than blocking the agent loop.
- Missing or invalid model-family rules fall back to the default token estimate.
- `ngettext()` before `setup_i18n()` returns English singular/plural based on `n`.

## i18n Artifacts

The current baseline fails because `src/iac_code/i18n/messages.pot` is required by tests but ignored by `.gitignore` through `*.pot`. This fix should make `src/iac_code/i18n/messages.pot` a tracked exception while keeping unrelated `.pot` files ignored.

After changing plural strings, run the existing translation flow so:

- `messages.pot` includes plural entries;
- each locale `.po` has matching plural translations;
- `.mo` files are compiled and current.

## Tests

Add or update focused pytest coverage:

- `tests/ui/suggestions/test_shell_history_provider.py`
  - repeated `provide()` calls for an unchanged file do not reread history;
  - changing size or mtime refreshes cached entries;
  - result count is capped.
- `tests/commands/test_model.py`
  - custom `apiBase` telemetry is detected when `activeProvider` is a string key.
- `tests/commands/test_status.py`
  - compact thousand formatting avoids floor-division precision loss.
- `tests/services/test_context_manager.py`
  - tool definition tokens appear in usage breakdown;
  - total tokens and compaction checks include tool definitions.
- `tests/services/test_token_counter.py`
  - tool definitions can be counted;
  - non-OpenAI model-family fallback handles Chinese-heavy text.
- `tests/ui/dialogs/test_resume_picker.py`
  - relative time, message count, and scroll markers use plural-aware strings.
- `tests/test_i18n.py`
  - existing completeness and compilation tests pass with the tracked `messages.pot`.

Run at minimum the targeted tests above. Because this touches shared status, i18n, and context accounting paths, finish with `make test` and `make lint` if the full test suite is not blocked by external environment problems.

## Acceptance Criteria

- #92 and the shell-history part of #84 are fixed without changing suggestion UI behavior.
- `/model` telemetry reports custom base URL presence and host kind when configured.
- `/status` compact formatting no longer floors 1,500 tokens to `1k`.
- Resume picker pluralized strings use gettext plural forms.
- `/status` context usage includes tool definition tokens.
- Context compaction thresholds use the same total displayed by `/status`.
- Non-OpenAI model-family token estimates are more conservative for Chinese-heavy text without new dependencies.
- i18n tests no longer fail because `messages.pot` is missing.
