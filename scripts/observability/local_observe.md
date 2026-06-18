# Local Observe Tool

`local_observe` is a small local OTLP receiver and web UI for testing `iac-code` telemetry.

## Start

```bash
uv run python scripts/observability/local_observe.py --port 4318 --no-open
```

Open `http://127.0.0.1:4318`.

## Send Telemetry To It

In the shell where you run `iac-code`:

```bash
export IAC_CODE_TELEMETRY_ENDPOINT=http://127.0.0.1:4318
export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1
unset DISABLE_TELEMETRY
```

To include raw prompts, model output, and tool payloads:

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT
```

Then run `iac-code` normally.

## UI

- Use **Demo debug off/on** to load sample data.
- Use **Expected raw content** to check debug-off/debug-on behavior.
- Pipeline records are grouped by run, step, AgentLoop round, and raw evidence.
- Pipeline-level evidence is split into lifecycle, normal chat after pipeline, and other session evidence.
- Use **Clear** before a new manual test.
- Use **Export JSONL** to save the captured records.
