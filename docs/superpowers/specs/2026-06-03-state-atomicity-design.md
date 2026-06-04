# State Atomicity Design

## Context

GitHub issue #80 reports four related reliability bugs in file persistence and version handling:

1. `session_index._decode_json_string` corrupts literal backslash sequences in its fallback decoder.
2. `config._save_yaml` writes YAML files directly, so an interrupted or racing write can leave partial data.
3. `update_checker._is_newer_version` falls back to lexicographic comparison for invalid versions.
4. `InputHistory._save` truncates the history file before rewriting it.

This design intentionally fixes only those four issue paths. Other direct writes found in the repository remain out of scope.

## Goals

- Preserve literal JSON escape sequences correctly when session metadata scanning falls back from `json.loads`.
- Make settings, credentials, cloud credential, telemetry identity, and input history writes atomic at the replacement boundary.
- Avoid false update notifications from invalid version string comparisons.
- Add focused regression tests for all four issue reports.

## Non-Goals

- Do not add cross-process merge or locking semantics for `config._save_yaml`.
- Do not convert unrelated persistence paths to atomic writes.
- Do not migrate existing settings, credentials, or history file formats.
- Do not change update source discovery or release selection logic beyond invalid-version comparison.

## Architecture

Add a small internal text atomic-write helper near the existing file security helpers. The helper writes content to a temporary file in the destination directory, flushes and fsyncs it, then replaces the final path via the existing `safe_replace` wrapper. It cleans up the temporary file if any step fails.

Callers keep their existing ownership of formatting and permissions:

- `config._save_yaml` serializes YAML, ensures the parent directory is private, calls the atomic text writer, then restricts the final file.
- `InputHistory._save` serializes the complete JSONL history content in memory, calls the atomic text writer, then restricts the final file.
- `update_checker` keeps its current update-state-specific atomic YAML writer unchanged.
- `session_index` keeps its fast string scanning behavior unchanged.

## Component Design

### Atomic Text Writes

The helper accepts a `Path` plus final text content and expects the destination parent directory to already exist. It creates a temporary file in that same directory with a dot-prefixed name and `.tmp` suffix, writes UTF-8 text, flushes, fsyncs, and replaces the target using `safe_replace`.

This keeps all replacement behavior in one place while preserving the existing `safe_replace` Windows retry behavior.

### Session Index Decode Fallback

`_decode_json_string` continues to try `json.loads` first. If the scanned JSON string body is truncated and `json.loads` fails, the fallback unescape order changes so escaped backslashes are processed before newline, tab, and quote escapes.

That means a truncated string fragment containing literal `\\n` remains backslash plus `n`, while a normal escaped newline `\n` still becomes a newline in fallback mode.

### YAML Config Save

`_save_yaml` keeps its current YAML formatting options:

- `default_flow_style=False`
- `allow_unicode=True`

It replaces `Path.write_text` with the atomic text writer. Parent directory creation and final permission restriction remain exactly where they are today.

### Update Version Comparison

`_is_newer_version` continues to compare valid PEP 440 versions with `packaging.version.Version`. If either candidate is invalid and raises `InvalidVersion`, the function returns `False`.

This favors avoiding false positives over attempting to infer ordering from arbitrary version strings.

### Input History Save

`InputHistory._save` builds the complete JSONL payload from all non-session-only entries before touching the file. It then atomically replaces the history file with that complete payload.

The JSONL format remains unchanged: one marked JSON object per persisted entry, with legacy plain-line loading support unchanged.

## Error Handling

The atomic text writer removes the temporary file on exceptions and re-raises the original error. Existing caller behavior remains visible to callers.

Permission restriction stays best-effort through existing file-security helpers. No advisory locks are added; atomic replacement prevents partial files but does not attempt to merge concurrent read-modify-write updates.

Invalid versions in update checking are treated as not newer. This prevents spurious update prompts caused by lexicographic ordering.

## Testing

Add or update focused tests:

- `tests/services/test_session_index.py`: a truncated JSON string fallback test where `\\n` stays literal and `\n` still decodes as newline.
- `tests/test_config.py`: `_save_yaml` uses a same-directory temp file, fsyncs, calls `safe_replace`, writes valid YAML, preserves owner-only permissions, and leaves no temp file behind.
- `tests/services/test_update_checker.py`: invalid version strings do not fall back to lexicographic comparison, including a case equivalent to `9.0.0-local` versus `10.0.0-local`.
- `tests/ui/core/test_input_history.py`: `_save` uses same-directory temp replacement, preserves reload behavior, and leaves no temp file behind.

Run the relevant tests first:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/services/test_session_index.py tests/test_config.py tests/services/test_update_checker.py tests/ui/core/test_input_history.py -q
```

If a full test run is needed, run:

```bash
PATH="$HOME/.local/bin:$PATH" make test
```

The current worktree baseline has a known unrelated failure: `src/iac_code/i18n/messages.pot` is missing, causing four `tests/test_i18n.py` failures. Any final verification report should separate those baseline failures from this issue's regression tests.
