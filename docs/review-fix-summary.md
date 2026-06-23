# Review Fix Summary

This document maps every current item in `docs/review.md` to its repair status, test coverage, and remaining risk.

## Critical

| Review item | Resolution | Tests | Residual risk |
| --- | --- | --- | --- |
| Critical 1 A2A delivered-but-not-recoverable event | Fixed by a centralized durable event classifier, durable journal/snapshot gate, and durable state I/O for recovery-semantic events. | `tests/a2a/test_pipeline_stream.py`, `tests/a2a/test_pipeline_journal.py`, `tests/a2a/test_pipeline_snapshot.py` | None |
| Critical 2 Windows `read_file` path check | Fixed by Windows-aware case and separator normalization before containment checks. | `tests/tools/test_read_file.py` | None |

## Major

| Review item | Resolution | Tests | Residual risk |
| --- | --- | --- | --- |
| Major 1 active A2A sidecar mismatch can discard recoverable pipeline | Fixed by returning recoverable task metadata for active mismatches and preserving the sidecar. | `tests/a2a/test_app.py`, `tests/a2a/test_pipeline_recovery.py` | None |
| Major 2 A2A handoff cleanup data incomplete | Fixed by using the private cleanup ledger as service-side source of truth and surfacing cleanup state unavailable when the ledger cannot be read. | `tests/a2a/test_pipeline_recovery.py`, `tests/ui/test_repl_pipeline_handoff.py` | None |
| Major 3 cleanup ledger state can be lost or regressed | Fixed by in-process ledger serialization, monotonic merge rules, persisted tool-use mappings, and mismatch rejection. | `tests/pipeline/engine/test_cleanup.py`, `tests/pipeline/engine/test_pipeline_runner_cleanup.py` | None |
| Major 4 pipeline persistence failures swallowed after in-memory advance | Fixed by making critical sidecar save failures hard pipeline errors before downstream work or success events continue. | `tests/pipeline/engine/test_pipeline_runner.py`, `tests/pipeline/engine/test_pipeline_runner_interrupt.py` | None |
| Major 5 cancel handoff is not an atomic event group | Fixed by durable `append_many()` cancel/handoff grouping and replay expansion. | `tests/a2a/test_pipeline_journal.py`, `tests/a2a/test_pipeline_recovery.py` | None |
| Major 7 `ToolContext` positional compatibility | Fixed by a compatibility `__init__` that preserves `ToolContext(cwd, event_queue, tool_use_id)`. | `tests/tools/test_tool_context.py`, `tests/tools/test_base.py` | None |
| Major 8 A2A image error and image placeholder lack i18n | Fixed by English `_()` msgids, `.format(...)`, shared image placeholder, and refreshed catalogs. | `make translate`, `tests/a2a/test_executor.py` | None |
| Major 9 cleanup visible strings use Chinese source msgids | Fixed by converting cleanup UI/prompt source msgids to English and moving Chinese text to `zh` catalog. | `make translate`, `tests/ui/test_repl_integration.py`, `tests/ui/test_repl_pipeline_handoff.py` | None |
| Major 11 `/resume` damaged sidecar meta fallback | Fixed by catching sidecar metadata read/parse failures and falling back without crashing. | `tests/ui/test_repl_integration.py`, `tests/pipeline/engine/test_pipeline_runner.py` | None |
| Major 13 corrupt cleanup ledger silently no-ops | Fixed by fail-closed load behavior, unavailable-state surfacing, and no overwrite of corrupt ledgers. | `tests/pipeline/engine/test_cleanup.py`, `tests/a2a/test_pipeline_recovery.py` | None |
| Major 14 cloud resource creation to ledger observation crash window | Accepted residual risk. The existing synchronous `ResourceObservedEvent` path, ledger write retry, and explicit failure surfacing remain the mitigation for this round. | `tests/pipeline/engine/test_pipeline_runner.py`, `tests/tools/cloud/test_base_stack.py` | A crash after cloud API success and before observed-resource ledger persistence can still leave a resource untracked. |
| Major 15 Windows cleanup replace and Selling Console socket behavior | Fixed by shared replace retry/state I/O paths and disabling unsafe Selling Console address reuse on Windows. | `tests/utils/test_state_io.py`, `tests/a2a/test_selling_console_script.py` | None |
| Major 16 completion guard and pipeline schema documentation | Fixed by adding `docs/pipeline-schema-reference.md`. | Documentation review | None |

## Minor

| Review item | Resolution | Tests | Residual risk |
| --- | --- | --- | --- |
| Minor 1 `--default-cwd` docs mismatch | Fixed docs to describe allowed-root directory creation and rejection conditions. | Documentation review | None |
| Minor 2 A2A image limits missing from docs | Fixed docs for MIME types, exact parser size limits, and `file://` checks requiring both request-cwd and allowed-root containment. | Documentation review | None |
| Minor 3 Selling Console and A2A image capability mismatch | Fixed docs and script docstring to state Selling Console is text-only and debugger covers image parts. | `tests/a2a/test_selling_console_script.py` | None |
| Minor 4 stale pipeline-image worktree path | Fixed manual guide to use the current repository checkout/worktree root. | Documentation review | None |
| Minor 5 REPL E2E English/POSIX/system-temp docs | Fixed by adding English README and updating Chinese README to say POSIX-only and system temporary directory. | Documentation review | None |
| Minor 6 legacy cleanup prompt detection depends on Chinese text | Fixed by central cleanup metadata constant and broader legacy cleanup prompt heuristics. | `tests/services/test_session_index.py` | None |
| Minor 7 duplicate `CLEANUP_PROMPT_METADATA_TYPE` definitions | Fixed by centralizing the constant in a low-dependency pipeline constants module. | `tests/services/test_session_storage.py`, `tests/services/test_session_index.py` | None |
| Minor 8 empty `stack_id` emits observed resource | Fixed by skipping `ResourceObservedEvent` when the stack id is empty. | `tests/tools/cloud/test_base_stack.py` | None |
| Minor 9 completion guard state lacks logs | Fixed by warning logs for parse and rebuild failures. | `tests/pipeline/engine/test_recovery.py` | None |
| Minor 10 cleanup event names are stringly typed | Fixed by centralizing cleanup event constants. | `tests/a2a/test_pipeline_recovery.py`, `tests/pipeline/engine/test_cleanup.py` | None |
| Minor 11 debugger and Selling Console ignore delivery aliases | Fixed by reading `deliveryTaskId` and `deliveryContextId` aliases in debugger and console UI. | `tests/a2a/test_pipeline_debugger_script.py`, `tests/a2a/test_selling_console_script.py` | None |
| Minor 12 normal-mode cleanup scans and session save overhead | Fixed by opt-in cleanup prompt preservation and reduced cleanup scan behavior. | `tests/services/test_session_storage.py`, `tests/ui/test_repl_pipeline_handoff.py` | None |
| Minor 13 duplicate `_pipeline_mode` assignment | Fixed by removing the duplicate assignment. | `make lint` | None |
| Minor 14 deploying hook skips missing `from_attempt_id` silently | Fixed by logging a warning when cleanup hook data is missing. | `tests/pipeline/selling/test_deploying_cleanup_hook.py` | None |
| Minor 15 `set.update(dict)` readability | Fixed by updating with `precompleted_tools.keys()` explicitly. | `tests/pipeline/engine/test_step_executor.py` | None |
| Minor 16 cleanup and Selling Console docstrings | Fixed by documenting the cleanup state model and Selling Console text-input scope. | Documentation/code review | None |
| Minor 17 miscellaneous docs gaps | Fixed `scripts/README.md`, batch docs, tiktoken fixture note, `pexpect` POSIX note, and template wording. | Documentation review | None |
| Minor 18 Windows helper script details | Fixed POSIX guard, Windows-safe command splitting, Selling Console shutdown/address reuse behavior, and documented dot-prefix temp-file semantics. | `tests/repl_e2e/test_run_pipeline_scenarios.py`, `tests/a2a/test_selling_console_script.py` | None |

## Historical Hardening

| Review item | Resolution | Tests | Residual risk |
| --- | --- | --- | --- |
| Historical 1 sidecar `context.yaml` and `meta.yaml` two-file consistency | Accepted residual risk. Single-file writes are now atomic and durable, but the two sidecar files are not linked by a generation/checksum in this batch. | `tests/pipeline/engine/test_session.py`, `tests/pipeline/engine/test_pipeline_runner.py` | Crash between the two sidecar writes can still leave the pair out of sync. |
| Historical 2 A2A journal append lacks fsync and snapshot lacks parent fsync | Fixed by durable journal appends, `append_many()`, atomic snapshot writes, and best-effort parent-directory fsync. | `tests/a2a/test_pipeline_journal.py`, `tests/a2a/test_pipeline_snapshot.py`, `tests/utils/test_state_io.py` | None |
| Historical 3 `SessionStorage.save()` truncate-and-rewrite | Fixed by atomic full-file save, opt-in cleanup prompt preservation, and locked JSONL append helper. | `tests/services/test_session_storage.py`, `tests/utils/test_state_io.py` | None |
| Historical 4 A2A snapshot `Path.replace()` Windows lock behavior | Fixed through shared safe replace retry behavior. | `tests/a2a/test_pipeline_snapshot.py`, `tests/utils/test_state_io.py` | None |
| Historical 5 legacy session migration `shutil.move()` target behavior | Fixed by using the shared safe replace path and leaving existing directory-format sessions authoritative. | `tests/services/test_session_storage.py` | None |
| Historical 6 Windows signal handler fallback | Fixed by guarding unsupported `loop.add_signal_handler` behavior, including `RuntimeError`. | `tests/utils/test_signals.py`, `tests/ui/test_repl_integration.py` | None |
| Historical 7 image store Windows privacy behavior | Accepted residual risk. POSIX stores use private dirs and `0600` file mode; the Python standard library path used here does not enforce an equivalent Windows ACL without platform-specific extensions. | `tests/utils/test_file_security.py`, documentation review | Windows image-cache privacy depends on the user's profile/config directory ACLs. |
| Historical 8 Session JSONL concurrent append on Windows | Fixed by `append_jsonl_locked()` using process-local serialization plus platform locks (`fcntl` on POSIX, `msvcrt` on Windows). | `tests/utils/test_state_io.py`, `tests/services/test_session_storage.py` | None |
