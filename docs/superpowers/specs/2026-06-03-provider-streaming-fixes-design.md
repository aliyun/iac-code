# Provider Streaming Fixes Design

Date: 2026-06-03
Branch: provider-streaming
Issue: GitHub issue #67

## Goal

Fix three provider streaming reliability bugs with the smallest practical change:

- A successful fallback completion must not permanently change the selected model or cached provider.
- A streaming request that stops yielding events must time out and recover instead of hanging forever.
- QwenPaw configuration errors during provider streaming must not call `sys.exit(1)` from provider-manager code.

The implementation should preserve existing public APIs, existing retry behavior, and the current stream-to-non-stream fallback behavior.

## Non-Goals

- Do not refactor provider selection, retry, or streaming into new subsystems.
- Do not change startup-time QwenPaw `SystemExit` handling in CLI, REPL, or agent factory code.
- Do not change the fallback model map or fallback selection policy.
- Do not introduce user-facing fallback notifications beyond existing telemetry and stream error behavior.

## Current Problems

### Permanent Fallback Downgrade

`ProviderManager._complete_with_retry()` mutates `self._model` and `self._provider` before invoking fallback. If fallback succeeds, those fields are not restored. A single transient primary-model failure can therefore silently switch the whole session to a weaker fallback model.

### Streaming Idle Hang

`ProviderManager.stream()` currently uses `async for event in provider.stream(...)` and calls `StreamWatchdog.ping()` only after an event arrives. If the provider stream awaits forever before yielding the next event, the watchdog never runs and the session can hang indefinitely.

### Provider-Layer `sys.exit`

`ProviderManager._check_qwenpaw_config_change()` catches `QwenPawError`, prints to stderr, and calls `sys.exit(1)`. This creates a process-level exit path inside provider-manager streaming code and bypasses normal stream error handling.

## Proposed Approach

Use a focused local patch in `ProviderManager` and tests.

### Fallback Completion

Change `_complete_with_retry()` so fallback completion uses a temporary provider instance instead of mutating manager state.

The primary provider and model remain cached on the manager. When retry on the primary provider exhausts and a fallback model exists, the manager creates a fallback provider and runs a single completion attempt through the same retry classification path. If fallback succeeds, return its `NonStreamingResponse` while leaving `self._model` and `self._provider` unchanged. If fallback fails, keep the existing behavior of raising the original primary failure.

This preserves the existing observable result of a successful fallback response while preventing session-wide model downgrade.

### Streaming Idle Timeout

Replace the bare streaming `async for` loop in `ProviderManager.stream()` with explicit async-iterator consumption. Each request for the next event is wrapped with `asyncio.wait_for(..., timeout=self._stream_idle_timeout)`.

If the next event does not arrive before the timeout, treat it as a streaming failure. The existing recovery path then:

- yields tombstones for any orphaned partial messages,
- calls non-streaming fallback via `_complete_with_retry()`,
- yields the completed response as stream events, or
- yields a non-retryable `ErrorEvent` if fallback completion also fails.

The existing `StreamWatchdog` class can remain for now. The important behavior change is that waiting for the next provider event is itself bounded.

### QwenPaw Streaming Error Handling

Change `_check_qwenpaw_config_change()` so it raises an exception instead of printing and calling `sys.exit(1)`.

In `ProviderManager.stream()`, catch that configuration exception at the start of the stream and yield a single `ErrorEvent` with `is_retryable=False`, then return. This keeps provider-manager code inside streaming semantics and avoids process-level exit during an in-flight request.

Startup-time QwenPaw handling in CLI, REPL, and agent factory remains unchanged because it is outside the scope of this issue.

## Data Flow

Primary non-streaming completion:

1. `complete()` calls `_complete_with_retry()`.
2. `_complete_with_retry()` uses the manager's current provider.
3. Retryable primary failures are retried according to `RetryConfig`.
4. If primary retries exhaust, a temporary fallback provider is created for this call.
5. Fallback success returns a response without changing manager state.
6. Fallback failure raises the original primary error.

Streaming completion:

1. `stream()` checks QwenPaw config.
2. If QwenPaw config fails, yield `ErrorEvent` and stop.
3. Otherwise, create the provider async iterator.
4. Await each `__anext__()` with the configured idle timeout.
5. Normal events are processed and yielded as before.
6. Timeout or streaming exception enters the existing tombstone plus non-streaming fallback path.

## Error Handling

- Primary retryable failures keep existing retry classification.
- Fallback failures preserve existing behavior by reporting the original primary failure.
- Streaming idle timeout is treated like any other streaming failure and uses the existing fallback path.
- QwenPaw config errors during streaming become non-retryable `ErrorEvent`s.
- `asyncio.CancelledError` should still propagate normally and must not be swallowed by timeout or fallback handling.

## Tests

Add focused tests under `tests/providers/test_manager.py`:

- Fallback completion success does not mutate `ProviderManager._model` or `ProviderManager._provider`.
- A hanging provider stream respects `stream_idle_timeout` and recovers through non-streaming fallback.
- QwenPaw config failure during `ProviderManager.stream()` yields a non-retryable `ErrorEvent` instead of raising `SystemExit`.

Keep existing `tests/providers/test_stream_watchdog.py` unless implementation changes require a small adjustment.

Verification commands:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py tests/providers/test_stream_watchdog.py -v
PATH="$HOME/.local/bin:$PATH" make test
```

## Acceptance Criteria

- A successful fallback response does not change `ProviderManager.get_model_name()`.
- A provider stream that yields no next event completes through the fallback path within `stream_idle_timeout` plus minimal scheduling overhead.
- QwenPaw streaming config errors produce one non-retryable `ErrorEvent` and no `SystemExit`.
- Relevant provider tests pass.
- Full test suite passes.
