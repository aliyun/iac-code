# Provider Streaming Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix issue #67 by preventing permanent fallback downgrade, bounding streaming idle waits, and removing provider-layer `sys.exit(1)` during streaming.

**Architecture:** Keep the change local to `ProviderManager` and provider tests. Fallback completion will accept a per-call provider/model override so fallback can be temporary. Streaming will consume the provider async iterator explicitly with `asyncio.wait_for` around each next-event await. QwenPaw stream-time config errors will be converted to a non-retryable stream error event.

**Tech Stack:** Python 3.10+, pytest, pytest-asyncio, uv, existing `ProviderManager`, `RetryConfig`, and stream event dataclasses.

---

## File Structure

- Modify `src/iac_code/providers/manager.py`
  - Add `asyncio` import.
  - Add a provider-manager exception for stream-time configuration failures.
  - Replace stream `async for` with explicit iterator consumption using `asyncio.wait_for`.
  - Make fallback completion use a temporary provider and model without mutating manager state.
- Modify `tests/providers/test_manager.py`
  - Add regression tests for fallback state preservation, idle stream timeout recovery, and QwenPaw stream error handling.
- Leave `src/iac_code/providers/stream_watchdog.py` unchanged unless focused tests prove a small compatibility tweak is needed.

---

### Task 1: Fallback Completion Preserves Manager State

**Files:**
- Modify: `tests/providers/test_manager.py`
- Modify: `src/iac_code/providers/manager.py`

- [ ] **Step 1: Write the failing test**

Add this test inside `TestProviderManagerCompleteRetry` in `tests/providers/test_manager.py`:

```python
async def test_fallback_success_does_not_mutate_manager_state(self, monkeypatch):
    from iac_code.providers.base import NonStreamingResponse
    from iac_code.providers.retry import RetryConfig
    from iac_code.types.stream_events import Usage

    class Status503Error(Exception):
        status_code = 503

    class FakeProvider:
        def __init__(self, model: str, *, fail: bool = False):
            self.model = model
            self.fail = fail

        def get_model_name(self) -> str:
            return self.model

        async def complete(self, messages, system, tools=None, max_tokens=8192):
            if self.fail:
                raise Status503Error("temporary outage")
            return NonStreamingResponse(
                message_id="fallback-response",
                text="fallback ok",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=2),
            )

    created_models: list[str] = []

    def fake_create_provider(model, credentials, *, base_url=None, provider_key_override=None):
        created_models.append(model)
        return FakeProvider(model, fail=model == "claude-sonnet-4-6")

    monkeypatch.setattr("iac_code.providers.manager.create_provider", fake_create_provider)
    mgr = ProviderManager(
        model="claude-sonnet-4-6",
        credentials={"anthropic": "k"},
        retry_config=RetryConfig(max_retries=0, base_delay=0, jitter_factor=0),
    )
    original_provider = mgr._provider

    response = await mgr.complete(messages=[Message.user("hi")], system="")

    assert response.text == "fallback ok"
    assert created_models == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    assert mgr.get_model_name() == "claude-sonnet-4-6"
    assert mgr._provider is original_provider
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerCompleteRetry::test_fallback_success_does_not_mutate_manager_state -v
```

Expected: FAIL because `mgr.get_model_name()` is `claude-haiku-4-5-20251001`.

- [ ] **Step 3: Implement temporary fallback provider support**

In `src/iac_code/providers/manager.py`, change `_complete_with_retry()` to accept optional provider/model override parameters:

```python
async def _complete_with_retry(
    self,
    messages,
    system,
    tools,
    max_tokens,
    is_fallback=False,
    provider_override: Provider | None = None,
    model_override: str | None = None,
) -> NonStreamingResponse:
    provider = provider_override or self._ensure_provider()
    model = model_override or self._model
    provider_name = type(provider).__name__.replace("Provider", "").lower()
    sanitized_model = sanitize_model_name(model)
```

Use `model` for telemetry in this method. In the fallback block, replace mutation of `self._model` and `self._provider` with:

```python
fallback_provider = create_provider(
    fallback,
    self._credentials,
    base_url=self._base_url_override,
    provider_key_override=self._provider_key_override,
)
try:
    return await self._complete_with_retry(
        messages,
        system,
        tools,
        max_tokens,
        is_fallback=True,
        provider_override=fallback_provider,
        model_override=fallback,
    )
except Exception:
    raise original_exc from None
```

Remove the old `original_model` and `original_provider` save/restore block.

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerCompleteRetry::test_fallback_success_does_not_mutate_manager_state -v
```

Expected: PASS.

---

### Task 2: Streaming Idle Timeout Recovers Through Fallback

**Files:**
- Modify: `tests/providers/test_manager.py`
- Modify: `src/iac_code/providers/manager.py`

- [ ] **Step 1: Write the failing test**

Add this test inside `TestProviderManagerStreaming` in `tests/providers/test_manager.py`:

```python
async def test_stream_idle_timeout_recovers_with_non_streaming_fallback(self):
    class HangingStreamProvider:
        def get_model_name(self) -> str:
            return "claude-sonnet-4-6"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            await asyncio.sleep(999)
            yield MessageEndEvent(stop_reason="never", usage=Usage())

        async def complete(self, messages, system, tools=None, max_tokens=8192):
            return NonStreamingResponse(
                message_id="fallback-after-timeout",
                text="recovered",
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=3, output_tokens=4),
            )

    mgr = ProviderManager(
        model="claude-sonnet-4-6",
        credentials={"anthropic": "k"},
        stream_idle_timeout=0.01,
    )
    mgr._provider = HangingStreamProvider()

    events = await asyncio.wait_for(
        _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys")),
        timeout=0.5,
    )

    assert [event.type for event in events] == ["message_start", "text_delta", "message_end"]
    assert events[0].message_id == "fallback-after-timeout"
    assert events[1].text == "recovered"
```

Also add this helper near the top of `tests/providers/test_manager.py`:

```python
async def _collect_stream_events(stream):
    return [event async for event in stream]
```

Add `import asyncio` at the top of `tests/providers/test_manager.py`.

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerStreaming::test_stream_idle_timeout_recovers_with_non_streaming_fallback -v
```

Expected: FAIL with an outer `asyncio.TimeoutError` because the manager does not bound waiting for the next stream event.

- [ ] **Step 3: Implement bounded stream iteration**

In `src/iac_code/providers/manager.py`, add:

```python
import asyncio
```

Replace the `async for event in provider.stream(messages, system, tools, max_tokens):` block with explicit iteration:

```python
stream_iter = provider.stream(messages, system, tools, max_tokens).__aiter__()
while True:
    try:
        event = await asyncio.wait_for(stream_iter.__anext__(), timeout=self._stream_idle_timeout)
    except StopAsyncIteration:
        break
    watchdog.ping()
    if isinstance(event, MessageStartEvent):
        orphaned_message_ids.append(event.message_id)
        span.set_attribute(GenAiAttr.RESPONSE_ID, event.message_id)
    elif isinstance(event, TextDeltaEvent) and not first_token_received:
        first_token_received = True
        ttft_ns = int((time.monotonic() - started) * 1_000_000_000)
        span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
    yield event
    if isinstance(event, MessageEndEvent):
        watchdog.stop()
        self._set_llm_response_span_attrs(span, event, self._model)
        self._emit_success_telemetry(provider_name, sanitized_model, started, event.usage)
        return
```

After the loop, set `streaming_failed = True` so a stream that ends without `MessageEndEvent` uses the existing fallback path:

```python
streaming_failed = True
```

Keep `except asyncio.CancelledError: raise` before the broad `except Exception`.

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerStreaming::test_stream_idle_timeout_recovers_with_non_streaming_fallback -v
```

Expected: PASS.

---

### Task 3: QwenPaw Streaming Config Errors Become Error Events

**Files:**
- Modify: `tests/providers/test_manager.py`
- Modify: `src/iac_code/providers/manager.py`

- [ ] **Step 1: Write the failing test**

Add this test inside `TestProviderManagerStreaming` in `tests/providers/test_manager.py`:

```python
async def test_qwenpaw_config_error_yields_error_event_instead_of_system_exit(self, monkeypatch):
    from iac_code.services.qwenpaw_source import QwenPawError

    monkeypatch.setattr(
        "iac_code.config._get_env_overrides",
        lambda: {"api_key": None, "model": None, "base_url": None, "provider_key": None},
    )
    monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
    monkeypatch.setattr(
        "iac_code.services.qwenpaw_source.load_from_qwenpaw",
        lambda: (_ for _ in ()).throw(QwenPawError("bad qwenpaw config")),
    )

    mgr = ProviderManager(model="claude-sonnet-4-6", credentials={"anthropic": "k"})

    events = await _collect_stream_events(mgr.stream(messages=[Message.user("hi")], system="sys"))

    assert len(events) == 1
    assert events[0].type == "error"
    assert "bad qwenpaw config" in events[0].error
    assert events[0].is_retryable is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerStreaming::test_qwenpaw_config_error_yields_error_event_instead_of_system_exit -v
```

Expected: FAIL because `SystemExit(1)` is raised instead of yielding an `ErrorEvent`.

- [ ] **Step 3: Implement stream-time configuration exception handling**

In `src/iac_code/providers/manager.py`, add this exception near `ProviderNotConfiguredError`:

```python
class ProviderConfigurationError(RuntimeError):
    """Raised when provider configuration cannot be loaded during a request."""
```

Change `_check_qwenpaw_config_change()` so the `except QwenPawError` block becomes:

```python
except QwenPawError as exc:
    raise ProviderConfigurationError(str(exc)) from exc
```

At the start of `stream()`, replace the direct call with:

```python
try:
    self._check_qwenpaw_config_change()
except ProviderConfigurationError as exc:
    yield ErrorEvent(error=f"{type(exc).__name__}: {str(exc)[:1000]}", is_retryable=False)
    return
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py::TestProviderManagerStreaming::test_qwenpaw_config_error_yields_error_event_instead_of_system_exit -v
```

Expected: PASS.

---

### Task 4: Verification and Commit

**Files:**
- Verify: `src/iac_code/providers/manager.py`
- Verify: `tests/providers/test_manager.py`

- [ ] **Step 1: Run provider-focused test suite**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/providers/test_manager.py tests/providers/test_stream_watchdog.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" make test
```

Expected: all tests PASS.

- [ ] **Step 3: Check git diff**

Run:

```bash
git status --short
git diff -- src/iac_code/providers/manager.py tests/providers/test_manager.py
```

Expected: only planned source and test files are modified, plus this plan file if not committed yet.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add src/iac_code/providers/manager.py tests/providers/test_manager.py
git add -f docs/superpowers/plans/2026-06-03-provider-streaming-fixes.md
git commit -m "fix: stabilize provider streaming fallback"
```

Expected: commit succeeds after hooks pass.

---

## Self-Review

- Spec coverage: Task 1 covers temporary fallback state. Task 2 covers stream idle hangs. Task 3 covers QwenPaw provider-layer `sys.exit`. Task 4 covers verification.
- Placeholder scan: no placeholder words or incomplete steps.
- Type consistency: tests use existing `ProviderManager`, `Message`, `NonStreamingResponse`, `Usage`, and stream event fields already present in the codebase.
