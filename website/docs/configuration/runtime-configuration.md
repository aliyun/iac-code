---
title: Configuration
description: Runtime configuration order and local files.
---

# Configuration

IaC Code reads configuration from CLI arguments, environment variables, and files in the runtime configuration directory.

Configuration precedence:

```text
CLI arguments > environment variables > configuration files
```

The runtime directory defaults to:

```text
~/.iac-code/
```

You can relocate it by setting the `IAC_CODE_CONFIG_DIR` environment variable (supports `~` and `$VAR` expansion). When set, every persisted artifact — credentials, settings, history, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — follows the new location.

Common files:

| File | Description |
|---|---|
| `.credentials.yml` | LLM credentials |
| `.cloud-credentials.yml` | Cloud provider credentials |
| `settings.yml` | Selected provider, model, and related settings |
| `IAC-CODE.md` | User memory loaded as persistent instructions |
| history files | Input history for interactive workflows |

Avoid committing or sharing files from this directory because they can contain secrets or local preferences.

## Memory Files

IaC Code has two public memory locations:

| Location | Purpose |
|---|---|
| `<project-root>/IAC-CODE.md` | Project memory. This can be committed when the instructions are useful for everyone working in the project. |
| `<config-dir>/IAC-CODE.md` | User memory. This follows `IAC_CODE_CONFIG_DIR` and is private to the local user. |

Project auto-memory topic files are stored under:

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` in that folder is the topic index. When auto-memory is on, IaC Code may use a side call to select relevant topic files and add them as hidden conversation context.

## Project Settings

In addition to the user-level `~/.iac-code/settings.yml`, IaC Code loads project-level settings from the current working directory:

| File | Scope |
|---|---|
| `.iac-code/settings.yml` | Shared project settings (safe to commit). |
| `.iac-code/settings.local.yml` | Local overrides (should be git-ignored). |

Merge order: **user settings → project settings → project local settings → CLI arguments** (later sources override earlier ones).

## Tool Permission Configuration

The `permissions` section in `settings.yml` configures which tool actions are allowed, denied, or require confirmation:

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Field | Description |
|---|---|
| `mode` | Permission mode: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | List of tool permission patterns to auto-approve. |
| `deny` | List of tool permission patterns to auto-deny. |
| `ask` | List of tool permission patterns that always require confirmation. |
| `additional_directories` | Extra directories beyond cwd that the agent is allowed to write to. |

### Pattern Syntax

Tool permission patterns follow the format `tool_name(rule)`:

| Pattern | Meaning |
|---|---|
| `bash` | Match all bash commands (bare tool name). |
| `bash(git *)` | Match bash commands starting with `git`. |
| `bash(curl:*)` | Match bash commands starting with `curl`. |
| `write_file` | Match all write_file tool calls. |

Rules are evaluated in order: **deny → ask → allow → default behavior**. CLI arguments (`--allowed-tools`, `--disallowed-tools`) take the highest precedence.
