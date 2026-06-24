# Review2 Fixes Design

Date: 2026-06-24

## Goal

Close every remaining issue listed in `docs/review2.md` without widening the feature scope. The work is split into two implementation batches:

1. Reliability and user-visible correctness.
2. Maintainability and platform edge cases.

The final implementation should update code, tests, and review documentation so `docs/review2.md` no longer contains unresolved findings, and should produce a dedicated `docs/review2-fix-summary.md` closure document.

## Decisions

- `CleanupLedger` corruption handling uses a continue-with-warning policy.
  - Existing corrupt `cleanup.yaml` files must never be overwritten.
  - Pipeline execution should continue.
  - The runner must still make cleanup tracking unavailability visible through warning/event/observability paths.
- `AgentLoop.stamp_last_turn_elapsed()` must preserve cleanup prompts when it performs a full session save.
- All remaining minor findings in `docs/review2.md` should be fixed rather than left as accepted risks.
- The implementation should use small targeted changes and avoid broad refactors.

## Batch 1: Reliability And User-Visible Correctness

### Cleanup Ledger Corruption

Current issue: `CleanupLedger.record_observed()` and `CleanupLedger.mark_cleanup_required()` return silently when `_load_for_write()` detects a corrupt ledger. This keeps the corrupt file intact, but the caller cannot tell that cleanup tracking failed.

Design:

- Keep fail-closed write behavior: do not overwrite unreadable or invalid ledger content.
- Change key write methods so callers can distinguish successful writes, no-op input, and unavailable ledger state.
- Have `PipelineRunner` handle unavailable ledger state without failing the pipeline.
- Emit both of these signals when cleanup tracking is unavailable:
  - `logger.warning` with the ledger path, step id, attempted operation, and load error when available.
  - A non-terminal pipeline-visible warning event. If no suitable warning event type exists, add a narrowly scoped warning event type rather than overloading `STEP_FAILED` or `PIPELINE_ERROR`. The event must not change the pipeline terminal status.
- Warning event data must include a stable reason such as `cleanup_tracking_unavailable`, the ledger path, step id, operation name, and resource id/count when available.
- If a new warning event type is added, wire it through every user-visible pipeline event path that handles `PipelineEventType` values:
  - REPL rendering should show a non-terminal warning without marking the step or pipeline failed.
  - A2A `PipelineEventTranslator` should publish a warning envelope that Web UI clients can display.
  - A2A durable classification should explicitly decide the warning's recovery semantics. If the warning is needed after reconnect, classify it as recovery-semantic; otherwise document it as display-only.
  - Display replay and snapshot reducers should either record/display the warning or explicitly ignore it with tests proving no terminal state changes.

Tests:

- Corrupt ledger is not overwritten.
- Pipeline continues after a cleanup tracking unavailable condition.
- The logger warning is emitted.
- The non-terminal pipeline-visible warning event is emitted and does not make the pipeline fail.
- REPL and A2A translation paths surface or explicitly handle the warning event without treating it as terminal failure.

### Session Elapsed Stamp Preservation

Current issue: `stamp_last_turn_elapsed()` rewrites the full session file without `preserve_cleanup_prompts=True`. If the session file contains a cleanup prompt that is missing from the in-memory context, the elapsed stamp write can remove it.

Design:

- Call `SessionStorage.save(..., preserve_cleanup_prompts=True)` from `stamp_last_turn_elapsed()`.
- Keep the rest of the elapsed-stamp behavior unchanged.

Tests:

- Arrange a session where disk contains a cleanup prompt and memory messages do not.
- Run `stamp_last_turn_elapsed()`.
- Verify the assistant elapsed value is persisted and the cleanup prompt remains in the session.

### A2A Handoff Cleanup Documentation

Current issue: `docs/review-fix-summary.md` says the A2A handoff cleanup residual risk is "none", but the accepted design is more specific.

Design:

- Document that private cleanup ledger is the source of truth for server-side cleanup prompt semantics.
- Document that public A2A snapshot exists only for Web UI recovery.
- Document that if the private ledger is missing or unreadable, normal mode does not reconstruct a cleanup prompt from public snapshot or journal evidence; it exposes cleanup state unavailable instead.

### Selling Console Windows Socket Evidence

Current issue: the code has a Windows branch for `allow_reuse_address`, but tests do not directly prove that branch.

Design:

- Add a direct test that monkeypatches the platform to Windows and verifies the created server class disables address reuse.
- Keep the documented behavior and implementation unchanged unless the test reveals an implementation problem.

## Batch 2: Maintainability And Platform Edge Cases

### Cleanup Event Constants

Current issue: cleanup event constants exist but several production consumers still use hard-coded protocol strings.

Design:

- Replace production hard-coded cleanup event strings with constants from `iac_code.pipeline.constants`.
- Keep test literals where they assert protocol wire values.
- Ensure protocol values do not change.

### Cleanup Module Documentation

Current issue: `cleanup.py` lacks enough class and method documentation for the cleanup state model.

Design:

- Add concise docstrings for the core data classes and public ledger/observer methods.
- Explain state model, fail-closed behavior, and write semantics.
- Avoid long narrative comments or unrelated documentation churn.

### JSON-RPC Error Data Passthrough

Current issue: JSON-RPC error data passthrough monkey-patch logic is split across modules and one patch runs at import time.

Design:

- Introduce a small shared helper as the single installation path for passthrough behavior.
- Reuse it from both A2A app and pipeline executor code.
- Move installation to explicit A2A app/dispatcher startup paths. `pipeline_executor.py` must not install the monkey-patch merely by being imported.
- Preserve current behavior and idempotency.
- Avoid changing external JSON-RPC response shape except where existing passthrough behavior already does so.

Tests:

- Importing `iac_code.a2a.pipeline_executor` alone does not patch JSON-RPC response helpers.
- Explicit installation keeps recoverable error `data` passthrough behavior and remains idempotent.

### Path Lock Registry Growth

Current issue: `_PATH_LOCKS` and `_LEDGER_LOCKS` grow monotonically with path count in long-running processes.

Design:

- Replace the unbounded lock dictionaries with a reusable lock registry that cannot evict or replace a lock while it may still be held.
- Prefer weak-reference or reference-counted registry semantics over time/size-only eviction. A simple size cap is not acceptable if it can create a second live lock for the same path.
- Preserve per-path in-process serialization.
- Keep implementation simple and deterministic enough to test.

Tests:

- Same path returns the same lock while retained.
- A lock that is currently held remains the unique lock for that path.
- Stale locks can be released from the registry after no callers retain them.

### macOS Path Case Handling

Current issue: `_path_is_under()` case-normalizes Windows paths only. macOS default filesystems are often case-insensitive.

Design:

- Add a helper for case-insensitive path comparison when the underlying platform/path behavior requires it.
- Windows always uses case-insensitive comparison.
- macOS should use a conservative case-sensitivity probe for the relevant root path rather than assuming all macOS volumes are case-insensitive.
- Preserve behavior on case-sensitive POSIX filesystems.

### Legacy Session Cross-Filesystem Migration

Current issue: migration changed from `shutil.move()` to `safe_replace()`. `os.replace()` can fail across filesystems with `EXDEV`.

Design:

- Extend the shared replace/migration path to support cross-device fallback.
- On `EXDEV`, copy to the target, make the target durable/private, and unlink the legacy source only after the copy succeeds.
- Preserve Windows retry behavior for replace-related permission failures.

Tests:

- Simulate `EXDEV` and verify fallback copies content and removes the source.
- Verify normal replace path still works.

### Recovery Semantic Event Simplification

Current issue: `is_recovery_semantic_event()` contains an unreachable-looking final branch after earlier status checks.

Design:

- Simplify the predicate while preserving durable classification behavior.
- Add or update tests for the event/status/scope combinations that matter.

### Unused Cleanup Payload Parameter

Current issue: `_cleanup_payload_from_private_ledger_or_unavailable()` accepts `public_snapshot` but does not use it.

Design:

- Remove the unused parameter.
- Update call sites and tests.
- Keep the explicit design that public snapshot is not used to reconstruct server-side cleanup prompt semantics.

## Documentation Updates

Update `docs/review-fix-summary.md`:

- Correct A2A handoff cleanup residual risk wording.
- Correct Selling Console Windows socket test evidence after adding the direct test.
- Correct any status text affected by the implementation.

Update `docs/review2.md`:

- Fix the stale "4 Major" count after the removed `ToolContext` item.
- Mark each finding as fixed or moved into the implementation summary.
- Keep source attribution only where useful for traceability.

Create `docs/review2-fix-summary.md`:

- Use a structure similar to `docs/review-fix-summary.md`.
- Map every `docs/review2.md` finding to its final handling status.
- For each item, include the implementation summary, test evidence, and residual risk.
- Explicitly note any user-approved design decisions, especially A2A public snapshot being Web UI recovery-only and cleanup ledger corruption continuing the pipeline with visible warning.
- Keep this as the authoritative closure document for the review2 fix batch.

## Verification Plan

Run focused tests first:

- `tests/pipeline/engine/test_cleanup.py`
- `tests/pipeline/engine/test_pipeline_runner_cleanup.py`
- `tests/services/test_session_storage.py`
- `tests/a2a/test_selling_console_script.py`
- `tests/utils/test_state_io.py`
- `tests/tools/test_read_file.py`
- A2A tests covering JSON-RPC passthrough and cleanup payload behavior
- Documentation checks for `docs/review2.md`, `docs/review-fix-summary.md`, and `docs/review2-fix-summary.md`

Then run:

- `make test`

If the full suite cannot run in the available environment, record the reason and list the focused tests that did run.

## Non-Goals

- Do not change A2A public snapshot into a server-side cleanup prompt source.
- Do not fail the pipeline solely because cleanup ledger tracking is unavailable.
- Do not redesign session storage beyond the specific preservation and migration fixes.
- Do not introduce platform-specific dependencies for Windows ACL behavior.
