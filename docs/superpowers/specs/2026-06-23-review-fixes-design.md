# Review Fixes Design

## Context

This design covers all actionable items in `docs/review.md`, including the section labeled `原本就有的问题`. The work will be implemented as one coordinated design split into multiple implementation batches.

The review spans A2A pipeline recovery, A2A event durability, cleanup ledger correctness, pipeline sidecar persistence, Windows compatibility, i18n, documentation, and minor cleanup items. The implementation must stay within existing module boundaries and avoid unrelated refactors.

## Goals

- Fix every current `Critical`, `Major`, and `Minor` item in `docs/review.md`.
- Include the historical hardening items listed in `原本就有的问题`, with one explicitly accepted residual risk for sidecar two-file consistency.
- Preserve normal mode behavior unless the review item explicitly targets normal mode performance or shared infrastructure.
- Avoid real LLM, real cloud, or real network requirements in tests.
- Produce a closure document summarizing each review item as fixed or accepted residual risk.

## Non-Goals

- Do not merge `context.yaml` and `meta.yaml` into a single sidecar state file.
- Do not add generation/checksum consistency between sidecar `context.yaml` and `meta.yaml` in this round.
- Do not introduce a cleanup append-only journal.
- Do not add cross-process file locking for `cleanup.yaml`; the supported model is one process writing a ledger.
- Do not make the real PTY REPL E2E runner support Windows. It will be documented and guarded as POSIX-only.
- Do not rewrite the entire pipeline documentation system beyond the schema/reference and concrete gaps called out in review.

## Accepted Residual Risk

`PipelineSession` will continue to store sidecar state in two files: `context.yaml` and `meta.yaml`. This round will improve single-file atomicity through a shared state I/O helper, but it will not add a shared generation/checksum or merge the files. A crash between the two file writes can still leave the pair out of sync. This is accepted for this batch and must be recorded in `docs/review-fix-summary.md`.

## Architecture

The design uses a shared state I/O layer and then applies fixes in dependent batches:

1. State I/O foundation.
2. A2A recovery and handoff semantics.
3. Cleanup ledger correctness.
4. Pipeline runner persistence behavior.
5. Windows, i18n, documentation, and minor closure.

The core reliability rule is:

- Events or files that affect recovery semantics must be persisted before they are treated as successful.
- Display-only streaming data can remain best-effort to preserve interactive latency.

## Batch 1: State I/O Foundation

### Atomic State Writes

Add a low-dependency helper for state-file writes. It should support:

- Write to a temporary file in the destination directory.
- Flush and optionally fsync file contents.
- Atomically replace the target.
- Best-effort fsync of the parent directory.
- Short retry around replace failures, especially for Windows file-lock behavior.
- Clear exceptions when writes cannot be made durable.

The helper will be used only for review-scoped paths:

- `pipeline/cleanup.yaml`
- A2A pipeline snapshot files
- `PipelineSession` sidecar `context.yaml` and `meta.yaml`
- `SessionStorage.save()` and related replace/move paths called out by review

This is not a whole-repository migration.

### A2A Journal Durability

Extend the A2A journal API with:

- `append(event, durable: bool = False)`
- `append_many(events, durable: bool = False)`

Durable appends must write, flush, and fsync. Best-effort appends can flush without fsync.

`append_many()` is required for event groups such as cancel plus handoff. The group should be written under one file open and fsync once when durable. If a durable group fails, callers must not treat any event in the group as successfully persisted or delivered.

### Session Storage Save

Keep `SessionStorage.save()` as a full-file save API. It still receives the complete message list and writes the complete JSONL session file.

Change the write path from truncate-and-rewrite to atomic replace. Cleanup prompt preservation becomes explicit and opt-in. Normal mode saves should not read the old session file just to scan for cleanup prompts. Preservation should be enabled only from flows that may rewrite or compact context while needing to retain hidden cleanup prompts.

### ToolContext Compatibility

Restore the historical positional argument contract:

1. `cwd`
2. `event_queue`
3. `tool_use_id`

Move newly added fields after `tool_use_id`, and prefer keyword usage for new fields. Add a regression test asserting `ToolContext("/tmp", None, "toolu-1").tool_use_id == "toolu-1"`.

### Windows Path Safety

Replace `read_file.py`'s local `_path_is_under()` behavior with the existing cross-platform path normalization from `tools/path_safety.py`, or make the local helper equivalent. It must handle Windows drive-letter case and separator normalization.

## Batch 2: A2A Recovery And Handoff Semantics

### Narrow Durable Event Model

Only events that change recovery semantics are durable. Display-only events remain best-effort.

Durable examples:

- pipeline started
- step start, completion, and failure
- candidate selection, completion, and failure
- `input_required`
- terminal task or pipeline states
- `pipeline_canceled`
- `pipeline_handoff_ready`
- cleanup state transitions
- artifact metadata creation

Best-effort examples:

- `text_delta`
- candidate detail display
- diagram display
- ordinary tool result display
- permission display

If an ordinary tool result implies recovery state, the recovery-relevant information should be extracted into a separate durable state event. The raw tool result can remain best-effort.

### Active Sidecar Task Mismatch

When a `running` or `waiting_input` sidecar exists and the incoming A2A request does not match the owner task, the executor must not clear the sidecar and must not start a new pipeline.

Return a JSON-RPC error, using the existing invalid-params style, with machine-readable error data:

- `recoverableTaskId`
- `contextId`
- `sidecarStatus`

The message should tell the client to resume the returned task id. Debugger and selling console should surface the returned task id clearly.

### Cancel Handoff Event Group

Persist `pipeline_canceled` and `pipeline_handoff_ready` as a durable event group through `journal.append_many()`. Snapshot reduction should happen after the complete group has been appended.

If the group cannot be persisted, the executor must not publish successful cancel or handoff state. Tests should cover failure injection between the two events and assert no durable state contains only `pipeline_canceled` without the corresponding handoff.

### Cleanup Handoff Source Of Truth

`pipeline/cleanup.yaml` is the only authoritative cleanup source for service-side cleanup prompt recovery.

The public A2A snapshot exists for Web UI recovery and display. It may contain public resource summaries and cleanup status, but it must not be used to reconstruct service-side cleanup prompt semantics.

Normal and cancel handoff should derive cleanup handoff data from the private ledger. If the ledger is missing or unreadable, the system should expose `cleanup state unavailable` rather than guessing from public snapshot resources.

## Batch 3: Cleanup Ledger Correctness

### In-Process Serialization

Serialize read-modify-write operations for the same cleanup ledger path within one process. Cross-process locking is out of scope.

### State Merge Rules

`mark_cleanup_required()` must merge with existing resources rather than replacing them blindly.

Rules:

- `completed` and `skipped` remain terminal.
- `started`, `in_progress`, and `failed` must not regress to `pending`.
- Execution fields such as `cleanup_tool_use_id`, `cleanup_action`, `progress_status`, `progress_percentage`, and `last_error` are preserved unless a deliberate status transition replaces them.
- Declarative fields such as reason, source step, and metadata can be refreshed.

### Persistent Tool-Use Mapping

When `CleanupObserver` observes DeleteStack or GetStack tool use, persist a minimal tool-use mapping in the ledger:

- `tool_use_id`
- provider
- resource type
- resource id
- region
- action
- sanitized input summary needed for matching

Tool results first use the in-memory mapping. If it is missing, they load the persisted mapping from the ledger. If no mapping exists, they log and record a cleanup history warning instead of guessing.

### Corrupt Ledger Behavior

Ledger parse or structure failure is fail-closed:

- Do not silently no-op.
- Do not overwrite the corrupt ledger.
- Do not create an empty replacement ledger.
- Do not inject automatic cleanup prompts from partial state.

REPL, A2A, and Web UI surfaces should expose cleanup state unavailable and instruct users to inspect the session file and cloud resources manually.

### Cloud Resource Observation Window

No additional recovery subsystem will be added for the small window between successful cloud API creation and observed-resource ledger persistence. The existing synchronous `ResourceObservedEvent` path, ledger write retry, and explicit failure surfacing are considered sufficient for this round.

## Batch 4: Pipeline Runner Persistence

Pipeline runner checkpoints that affect recovery semantics must not fail silently.

Use the shared retry helper for critical sidecar saves. If retries fail, pipeline execution stops before advancing to later steps or issuing further cloud operations. The user-facing error should clearly state that pipeline state persistence failed.

Tests should inject sidecar save failures and assert:

- downstream steps or cloud tools are not executed after persistent failure
- REPL/A2A surfaces receive a clear error
- retry success allows normal continuation

The sidecar two-file consistency residual risk remains accepted and documented.

## Batch 5: Windows, I18n, Docs, And Minor Closure

### Windows

Runtime paths should be cross-platform. Real PTY REPL E2E scripts can be POSIX-only.

Runtime fixes include:

- path normalization for `read_file`
- atomic replace retry and explicit failure surfacing
- state-file write helper coverage for review-scoped files

Script and documentation fixes include:

- guard real PTY E2E runner on Windows with a clear error
- mark `pexpect` usage or runner docs as POSIX-only
- replace hard-coded `/tmp` docs with system temporary directory wording
- document Windows limitations for real PTY E2E

### I18n

Source msgids for user-visible UI, CLI, and A2A text should be English. Chinese text belongs in the Chinese catalog.

Fix:

- A2A image input errors
- `[Image input]`
- cleanup status, badge, and user-visible cleanup prompt text

Use `_("... {name} ...").format(name=...)`, not f-strings around `_()`. Update translation files through the project workflow.

### Documentation

Fix every documentation gap called out in `docs/review.md`:

- `--default-cwd` behavior and directory-creation side effect
- A2A image MIME, size, and `file://` safety limits
- Selling Console text-only capability versus A2A debugger image support
- stale pipeline-image worktree path
- REPL E2E English/POSIX/system-temp documentation
- VSwitch template commit documentation
- `pexpect` dev dependency mention
- scripts README entries

Add a formal pipeline schema reference covering at least:

- `completion_guards`
- `surface_overrides`
- `parameter_overrides`
- `a2a_artifacts`
- `exit_condition`
- `inject_tools`
- `ui_mode`
- `conclusion_schema`
- `interrupt_judge_failure`
- `hooks_file`
- `enabled_when`

### Minor Code Cleanup

Fix all remaining Minor items:

- centralize `CLEANUP_PROMPT_METADATA_TYPE`
- guard empty `stack_id` before emitting `ResourceObservedEvent`
- add useful completion guard logging
- centralize cleanup event names or enum-like constants
- display `deliveryTaskId` and `deliveryContextId` where relevant
- reduce unnecessary normal-mode cleanup scans
- remove duplicate `_pipeline_mode` assignment
- warn when deploying cleanup hook lacks `from_attempt_id`
- replace `set.update(dict)` with explicit key update
- add focused docstrings for cleanup state model and selling console script

### Closure Summary

Create `docs/review-fix-summary.md` after implementation. It should map each review item to:

- fixed
- test coverage
- accepted residual risk

The sidecar two-file consistency issue must be recorded as accepted residual risk.

## Testing Strategy

Add focused pytest coverage for each high-risk fix:

- durable A2A event failure behavior
- `append_many()` cancel handoff atomicity
- active sidecar mismatch error data
- cleanup ledger state merge
- cleanup persisted tool-use mapping
- corrupt cleanup ledger fail-closed behavior
- session atomic save and preservation on/off
- `ToolContext` positional compatibility
- Windows path normalization
- pipeline persistence retry/failure stop
- i18n string extraction patterns where feasible

Final verification should include:

- `make test`
- `make lint`

Real LLM, real cloud, and real PTY E2E are not required for this repair batch.

## Implementation Order

1. State I/O foundation.
2. A2A recovery and handoff semantics.
3. Cleanup ledger correctness.
4. Pipeline runner persistence.
5. Windows, i18n, docs, and minor closure.

Each batch should leave the test suite in a runnable state before moving to the next.
