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

This opens an interactive picker showing recent sessions for the current project, with the session name as the title when set, otherwise the last prompt or first prompt fallback.

To resume a specific session by exact session ID, unique ID prefix, or unique session name:

```text
/resume abc123
```

### Naming Sessions

Use `/rename` to give the active session a stable, human-readable name:

```text
/rename deploy-prod
```

The name is stored in the session metadata. It appears in the welcome banner when you resume, in the exit hint, and in the `/resume` picker.

You can resume by name when it uniquely identifies a session:

```text
/resume deploy-prod
iac-code --resume deploy-prod
```

### CLI: `--resume` and `--continue`

Resume a specific session from the command line by exact session ID, unique ID prefix, or unique session name:

```bash
iac-code --resume <session-id-or-name>
```

Resume the most recent session:

```bash
iac-code --continue
```

The short flags `-r` and `-c` are also available:

```bash
iac-code -r <session-id-or-name>
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
| Title | Session name when set, otherwise last user prompt or first prompt |
| Branch | Git branch at the time of the session |
| Time | Last modification time |

Sessions are sorted by most recent first. You can type to filter by title content.
