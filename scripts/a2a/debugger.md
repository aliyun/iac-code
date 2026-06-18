# A2A Pipeline Debugger

Short local guide for `scripts/a2a/debugger.py`.

## Start iac-code A2A

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299
```

For local smoke tests that should not stop on tool permissions:

```bash
cat >/tmp/iac-code-a2a-auto-approve.yml <<'EOF'
auto_approve_permissions: true
EOF

PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299 \
  --config /tmp/iac-code-a2a-auto-approve.yml
```

## Start the Debugger

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/debugger.py --port 41880 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
```

Open:

```text
http://127.0.0.1:41880
```

## Basic Flow

1. Click `Health` to verify the A2A server.
2. Enter a prompt and click `Stream`.
3. Use `Structured Pipeline` for the readable tree view.
4. Use `SSE Events` for raw event rows and expandable details.
5. Use `Fetch State` to load the current pipeline snapshot.
6. Use `Cancel` to cancel the active task.
7. Use `Export HTML` to share a read-only snapshot page.

## Logs and Replay

The debugger prints a per-run log directory at startup, for example:

```text
A2A pipeline debugger logs: /tmp/iac-code-a2a-debugger-runs/<run-id>
```

Replay a saved run:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/debugger.py --port 41880 \
  --load-log-dir /tmp/iac-code-a2a-debugger-runs/<run-id>
```

## Notes

- The debugger is a local development tool and does not provide authentication.
- `contextId` identifies the conversation; `taskId` identifies one A2A task.
- After `pipeline_handoff_ready`, follow-up messages normally start a new normal-chat task in the same context.
