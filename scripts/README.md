# Scripts

This directory contains local development, manual testing, and end-to-end helper scripts. Run scripts from the
repository root with `uv run python ...` unless a script-specific README says otherwise.

## Layout

| Path | Purpose |
| --- | --- |
| `a2a/debugger.py` | Web debugger/client for A2A pipeline streams. |
| `a2a/debugger.md` | Manual usage notes for the A2A debugger. |
| `a2a/e2e/` | A2A session recovery end-to-end scenario runner, shared helpers, and result notes. |
| `a2a/smoke/test_a2a_vpc.py` | Small manual smoke script for A2A VPC/pipeline behavior. |
| `acp/smoke/test_acp_vpc.py` | Small manual smoke script for ACP VPC behavior. |
| `headless/smoke/test_headless_vpc.py` | Small manual smoke script for headless VPC behavior. |
| `observability/local_observe.py` | Local OTLP observability server entrypoint. |
| `observability/local_observe/` | Local observe server implementation and static web UI. |
| `observability/local_observe.md` | Manual usage notes for the local observe tool. |
| `rendering/test_diagram_render.py` | Manual diagram rendering check. |

## Common Commands

```bash
uv run python scripts/a2a/debugger.py --help
uv run python scripts/a2a/e2e/run_recovery_scenarios.py --help
uv run python scripts/observability/local_observe.py --help
```

Pytest tests for these helpers live under `tests/`; the executable scripts here are kept for local debugging,
manual validation, and real end-to-end runs.
