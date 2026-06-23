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

`--default-cwd` is sent to the A2A server as `metadata.iac_code.cwd` on each
message. It is the server-side workspace for the task, not merely the debugger's
own working directory.

The A2A server validates this path before running the agent. By default, the
server accepts its own startup directory and Python's temp directory. If
`--default-cwd` points inside an allowed root and the directory does not exist
yet, the server may create it. The request is rejected with
`Invalid A2A workspace metadata.` when the resolved path escapes the allowed
root, cannot be created, or cannot be used as a directory.

Use one of these patterns:

```bash
# Start the debugger with a cwd accepted by the already-running server.
uv run python scripts/a2a/debugger.py --port 41880 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "/path/to/server/workspace"
```

```bash
# Or explicitly allow the debugger/client workspace when starting the server.
IACCODE_A2A_ALLOWED_CWDS="/path/to/server/workspace:/path/to/client/workspace" \
IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299
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
- Image input accepts supported image MIME types only: `image/png`, `image/jpeg`, `image/webp`, and `image/gif`.
- A2A part parser limits: text inline/raw and text `file://` parts are limited to 1 MiB; binary inline/raw/data parts are limited to 5 MiB; binary `file://` parts are limited to 25 MiB. Debugger uploads are limited to 5 MiB per image.
- `file://` image inputs must resolve to an existing local file that is both under the request cwd and under a configured A2A allowed cwd root. Local URLs outside either boundary are rejected.
- The A2A debugger sends image parts. The Selling Console web UI currently sends text input only.
