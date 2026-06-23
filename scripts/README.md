# Scripts

This directory contains local development, manual testing, and end-to-end helper scripts. Run scripts from the
repository root with `uv run python ...` unless a script-specific README says otherwise.

## Layout

| Path | Purpose |
| --- | --- |
| `a2a/debugger.py` | Web debugger/client for A2A pipeline streams. |
| `a2a/debugger.md` | Manual usage notes for the A2A debugger. |
| `a2a/selling_console.py` | Local HTTP server for the Selling Pipeline Console; proxies text-only UI requests to an A2A server. |
| `a2a/selling_console_web/` | Static Selling Console frontend. It renders pipeline progress, candidate cards, chat, and debug panels; image input coverage belongs to `a2a/debugger.py`. |
| `a2a/e2e/` | A2A session recovery end-to-end scenario runner, shared helpers, and result notes. |
| `a2a/smoke/test_a2a_vpc.py` | Small manual smoke script for A2A VPC/pipeline behavior. |
| `acp/smoke/test_acp_vpc.py` | Small manual smoke script for ACP VPC behavior. |
| `headless/smoke/test_headless_vpc.py` | Small manual smoke script for headless VPC behavior. |
| `observability/local_observe.py` | Local OTLP observability server entrypoint. |
| `observability/local_observe/` | Local observe server implementation and static web UI. |
| `observability/local_observe.md` | Manual usage notes for the local observe tool. |
| `rendering/test_diagram_render.py` | Manual diagram rendering check. |
| `repl/e2e/` | Real PTY-driven REPL pipeline end-to-end scenario runner. POSIX-only because it uses `pexpect`. |

## Common Commands

```bash
uv run python scripts/a2a/debugger.py --help
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/selling_console.py --port 41980 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
uv run python scripts/a2a/e2e/run_recovery_scenarios.py --help
uv run python scripts/observability/local_observe.py --help
uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

`scripts/repl/e2e/run_pipeline_scenarios.py` writes artifacts under the system temporary directory by default and is intended for manual or smoke validation. It is not part of `make test`; the unit tests only cover helper behavior. The real PTY runner depends on the POSIX-only `pexpect` development dependency.

The root `conftest.py` includes a tiktoken isolation fixture so tests do not read or write the developer's real encoding cache. Keep new tests on that fixture path rather than using the user cache directly.

Cleanup ledger temporary files use a leading dot in their generated names only as a cosmetic convention. Correctness relies on atomic replace, retries, and ledger validation, not on Unix hidden-file behavior.

Pytest tests for these helpers live under `tests/`; the executable scripts here are kept for local debugging,
manual validation, and real end-to-end runs.
