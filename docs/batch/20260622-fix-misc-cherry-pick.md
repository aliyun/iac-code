# 2026-06-22 fix-misc cherry-pick

## Summary

Cherry-picked the latest four commits from the `codex/fix-misc` worktree into
`fix_pipeline`:

- `ae75419 fix: refine pipeline memory policy`
- `e786fa5 fix: expose pipeline docs in website navigation`
- `5fd54e8 fix: remove static pipeline rollback rules`
- `84fd3fa fix: resolve pipeline skill reference paths`

## What Changed

### Pipeline Memory Policy

- Removed automatic full auto-memory injection from pipeline step agent loops.
- Kept memory access model-driven through explicit `read_memory` tool use.
- Added prompt guidance for intent parsing and architecture planning so those
  steps can choose memory when it is useful.
- Updated tests covering pipeline memory policy and REPL pipeline memory setup.

### Website Pipeline Documentation Navigation

- Added pipeline documentation entries to the website navbar and footer.
- Updated localized Docusaurus navbar/footer metadata.
- Added a navigation test to prevent pipeline docs from disappearing again.

### Pipeline Rollback Rules

- Removed static per-step rollback restrictions from the pipeline schema and
  selling pipeline configuration.
- Simplified rollback handling to rely on the supported dynamic rollback path.
- Updated engine, state machine, runner, and related tests for unrestricted
  rollback behavior.

### Pipeline Skill Reference Reads

- Added pipeline-only relative read roots so step agents can read skill
  reference files such as `references/template-parameters.md`.
- Kept normal REPL behavior unchanged: general trusted read roots do not change
  relative `read_file` path resolution.
- Preserved `ToolContext` positional compatibility after adding the new context
  field.
- Added regression tests for pipeline and non-pipeline read path boundaries.

## Conflict And i18n Notes

- The cherry-pick completed without code conflicts.
- No i18n catalog merge conflict occurred.
- `make test` detected that a new `read_memory` msgid was missing from the i18n
  catalogs. I ran `make translate`, confirmed the msgid was present in
  `messages.pot` and every `messages.po`, filled the new `msgstr` entry for
  `zh`, `es`, `fr`, `de`, `ja`, and `pt`, then ran `make translate` again to
  regenerate compiled catalogs without dropping existing entries.
- The website localization JSON files from the docs navigation commit were
  cherry-picked as committed.

## Verification

Run from `/Users/ehzyo/open_repo/iac-code3` after the i18n update:

```bash
make lint    # passed
make format  # passed, 740 files left unchanged
make test    # passed, 6663 tests
```
