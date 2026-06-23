# Selling Console Web

Standalone static frontend for `scripts/a2a/selling_console.py`. It is used to drive the selling pipeline, inspect step progress, select candidate plans, and continue into normal chat after deployment.

## Run

From the repository root, start the A2A server first:

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299
```

Then start the web console:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/selling_console.py --port 41980 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
```

Then open `http://127.0.0.1:41980`.

## Files

- `index.html` renders the page shell.
- `styles.css` contains layout, chat, plan cards, and progress visuals.
- `app.js` handles A2A stream parsing, UI state, debug controls, and interactions.
- `design/` keeps standalone visual explorations for progress variants.

The debug panel is collapsed by default. Expand it only when checking connection settings, progress variant parameters, context IDs, or recent stream diagnostics.
