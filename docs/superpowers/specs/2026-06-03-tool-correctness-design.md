# Tool Correctness Fixes Design

## Context

GitHub issue [#83](https://github.com/aliyun/iac-code/issues/83) reports three small correctness bugs in tool implementations:

1. `ReadMemoryTool.execute` and `WriteMemoryTool.execute` do not match the keyword-only `Tool.execute` contract.
2. The pure-Python grep fallback applies glob filters to filenames, while `rg --glob` applies them to relative paths.
3. `ListFilesTool` crashes when a directory contains a broken symlink because per-entry stat calls are not isolated.

This work intentionally leaves the pre-existing `messages.pot` baseline test failure untouched.

## Goals

- Make memory tool `execute` signatures conform to the base `Tool` interface.
- Make Python grep fallback glob matching consistent with ripgrep for path-aware patterns such as `src/**/*.py`.
- Make directory listing robust when individual entries cannot be stat'd.
- Add focused regression tests for each bug.

## Non-Goals

- Do not change memory read/write behavior beyond method signatures.
- Do not replace or broaden grep's ripgrep integration.
- Do not redesign `ListFilesTool` output formatting.
- Do not address unrelated i18n template generation or baseline test failures.

## Design

### Memory Tool Interface

Update both memory tool methods in `src/iac_code/memory/memory_tools.py` to include the keyword-only marker:

```python
async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
```

This keeps behavior unchanged while matching `Tool.execute` in `src/iac_code/tools/base.py`. Existing call sites already invoke tools with keyword arguments, so this is an interface correction rather than a call-flow change.

Regression coverage will inspect the method signatures and assert that `tool_input` and `context` are keyword-only parameters for both tools.

### Grep Python Fallback Glob Matching

Update `_python_grep` in `src/iac_code/tools/grep.py` so glob filtering checks the file path relative to the search root, not only the basename. For each file visited by `os.walk`, compute:

```python
relative_path = os.path.relpath(filepath, path)
```

Then apply `fnmatch.fnmatch(relative_path, glob)`.

This aligns fallback behavior with `rg --glob` for path patterns. The existing `*.py` behavior should remain covered because basename-style patterns should still match files at the root and, where Python `fnmatch` permits, nested relative paths ending in `.py`.

Regression coverage will create a nested `src/app.py` file and a non-matching file outside that path, then call `_python_grep(..., glob="src/**/*.py")` to verify the nested path pattern works without ripgrep.

### List Files Broken Symlink Handling

Update the per-entry loop in `src/iac_code/tools/list_files.py` so failures while checking an individual entry do not abort the whole listing. Directory detection and file size lookup should be wrapped in a narrow `try/except OSError` around the entry-specific filesystem calls. If an entry cannot be stat'd, skip it and continue.

This keeps the tool's existing result shape and avoids turning one broken entry into a full tool error.

Regression coverage will create a normal file and a broken symlink in a temporary directory, execute `ListFilesTool`, and assert:

- the result is successful,
- the normal file is still listed,
- the broken symlink does not crash the command.

## Testing

Run the focused tests for the modified behavior:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest \
  tests/memory/test_memory_tools.py \
  tests/tools/test_grep.py \
  tests/tools/test_list_files.py \
  -v
```

The full suite is known to have a pre-existing unrelated failure in `tests/test_i18n.py` because `src/iac_code/i18n/messages.pot` is missing and `*.pot` is ignored. That baseline failure is outside this issue's scope.
