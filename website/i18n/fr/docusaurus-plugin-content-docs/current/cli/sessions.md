---
title: Sessions
description: Persist and resume conversations across runs.
---

# Sessions

IaC Code automatically persists every conversation to disk. You can resume any previous session to continue where you left off.

## Resuming Sessions

### Interactive: `/resume`

In the REPL, use the `/resume` command:

```text
/resume
```

This opens an interactive picker showing recent sessions for the current project, with their last prompt as the title.

To resume a specific session by ID or ID prefix:

```text
/resume abc123
```

### CLI: `--resume` and `--continue`

Resume a specific session from the command line:

```bash
iac-code --resume <session-id>
```

Resume the most recent session:

```bash
iac-code --continue
```

The short flags `-r` and `-c` are also available:

```bash
iac-code -r <session-id>
iac-code -c
```

### Cross-project Sessions

When a session belongs to a different project directory, IaC Code does not hot-swap the working directory. Instead, it prints the command to resume in the correct context:

```text
cd /path/to/other/project && iac-code --resume <session-id>
```

This command is also copied to the clipboard when possible.

## Interruption Recovery

If a session was interrupted mid-execution (e.g., the process was killed while a tool was running), IaC Code detects the orphaned tool calls on resume and appends synthetic error results. This allows the model to recover gracefully without getting stuck waiting for tool output that will never arrive.

## Session Picker

The `/resume` picker displays:

| Column | Description |
|--------|-------------|
| Title | Last user prompt (or first prompt if no metadata) |
| Branch | Git branch at the time of the session |
| Time | Last modification time |

Sessions are sorted by most recent first. You can type to filter by title content.
