# Tool Correctness Fixes for GitHub Issue #83

## Summary

This batch fixes three tool implementation bugs reported in GitHub issue #83:

1. `ReadMemoryTool.execute` and `WriteMemoryTool.execute` now match the keyword-only `Tool.execute` interface contract.
2. `GrepTool` now keeps path glob behavior consistent between ripgrep and the Python fallback for patterns such as `src/**/*.py`.
3. `ListFilesTool` now skips entries that cannot be stat'd, including broken symlinks, instead of failing the whole directory listing.

## Changed Files

- `src/iac_code/memory/memory_tools.py`
- `src/iac_code/tools/grep.py`
- `src/iac_code/tools/list_files.py`
- `tests/memory/test_memory_tools.py`
- `tests/tools/test_grep.py`
- `tests/tools/test_list_files.py`
- `docs/superpowers/specs/2026-06-03-tool-correctness-design.md`

## Compatibility Notes

- Memory tools: all checked tool execution call sites use keyword arguments, so adding the keyword-only marker aligns the override with the base class without breaking normal dispatch.
- Grep tool: basename-only globs such as `*.py` continue to work across the searched tree. Path globs now use path-aware matching where `*` stays within one path segment and `**` can match zero or more path segments. When ripgrep is available and the user provides a path glob, the tool runs ripgrep from the search root and converts output back to absolute paths, preserving existing output shape.
- List files tool: top-level directory errors are still returned as tool errors. Only per-entry stat failures are skipped, so one broken symlink or inaccessible entry no longer prevents listing the rest of the directory.

## Conflict and Translation Handling

The cherry-pick did not require changes to user-facing translatable strings. No `messages.po` conflict was expected from this change set. If a future conflict touches translation files, preserve existing msgids and rerun `make translate` before completing the merge.

## Verification

Focused regression tests:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest \
  tests/memory/test_memory_tools.py \
  tests/tools/test_grep.py \
  tests/tools/test_list_files.py \
  -v
```

Lint/type checks:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH make lint
```

Whitespace check:

```bash
git diff --check
```
