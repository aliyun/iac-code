---
title: Command Line Options
description: Reference for IaC Code startup options and one-shot execution flags.
---

# Command Line Options

The root command starts Web by default. The options below cover Web startup, one-shot headless execution, and stream-json subprocesses. Configure interactive conversations in Web or Desktop.

The entry-point column shows where each option is accepted. `headless` means `iac-code --prompt ...`; `process` means stream-json subprocess mode. Web model and permission choices are configured in the interface.

| Option | Purpose | Entry point |
|---|---|---|
| `-h`, `--help` | Show CLI help and exit. Use this to inspect the options supported by your installed version. | `iac-code`, `web` |
| `-v`, `-V`, `--version` | Print the installed IaC Code version and exit. | `iac-code` |
| `-m <model>`, `--model <model>` | Start with a specific LLM model. This overrides the saved model for the current run. | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | Run a single prompt and exit. This enables non-interactive mode. Use `--prompt -` to read the prompt from standard input. | `iac-code` |
| `--output-format <format>` | Set output format for one-shot or process automation. Supported values are `text`, `json`, and `stream-json`. The default is `text`. | `headless`, `process` |
| `--input-format <format>` | Set input format. Supported values are `text` and `stream-json`. `stream-json` starts SDK process mode and must be combined with `--output-format stream-json`; it cannot be combined with `--prompt`. | `headless`, `process` |
| `--max-turns <number>` | Limit the maximum number of agent turns in non-interactive mode. The default is `100`. | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | Control whether one-shot non-interactive requests explicitly enable thinking. The default is `--thinking-enabled`; use `--no-thinking-enabled` to send `thinking_enabled=false` for this run without rewriting `settings.yml`. | `headless` |
| `-d`, `--debug` | Enable debug logging for this run. | `headless`, `process`, `web` |
| `--verbose` | Show headless (non-interactive) progress on standard error. | `headless` |
| `-r <session-id-or-name>`, `--resume <session-id-or-name>` | Resume by exact session ID, unique ID prefix, or unique name. Run headless commands from the session’s project directory. | `headless` |
| `-c`, `--continue` | Resume the most recent session. This cannot be used together with `--resume`. | `headless` |
| `--allowed-tools <patterns>` | Comma-separated tool permission patterns to allow, e.g. `'bash(git *),write_file'`. | `headless`, `process` |
| `--disallowed-tools <patterns>` | Comma-separated tool permission patterns to deny, e.g. `'bash(rm *)'`. | `headless`, `process` |
| `--permission-mode <mode>` | Permission mode: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. | `headless`, `process` |

## Permission Modes

The `--permission-mode` flag controls how the agent handles tool permission checks:

| Mode | Behavior |
|---|---|
| `default` | The agent prompts for confirmation when a tool action requires approval. |
| `accept_edits` | Auto-approve file system commands that are considered edits (e.g. `mkdir`, `cp`). Other actions still prompt. |
| `bypass_permissions` | Auto-approve tool actions except safety checks. Any allow decision that requires an audit record fails closed if audit persistence fails. Intended for trusted automation. |
| `dont_ask` | Deny tool actions that require approval unless an explicit allow rule applies. |

Alibaba Cloud write API calls made through `aliyun_api` are protected separately: a bare `aliyun_api` allow rule does not blanket-approve them, and outside `bypass_permissions` write allow rules must match the exact canonical `product:action` pair. `bypass_permissions` does auto-approve them, but like other audited allow decisions, the action is denied if the permission audit record cannot be persisted. Use exact rules such as `aliyun_api(ros:CreateStack)` when trusted automation should allow only a specific write API.

## Common Startup Commands

Start the local Web app:

```bash
iac-code
```

Start with a specific model for this run:

```bash
iac-code --prompt "Create a VPC" --model qwen3.7-max
```

Run a one-shot prompt:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Run a one-shot prompt with thinking disabled for this request:

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

Read the prompt from standard input:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Continue the latest session with a one-shot prompt:

```bash
iac-code --continue --prompt "Generate a VPC template"
```

Pre-approve bash commands matching `git *`:

```bash
iac-code --prompt "Show the git status" --allowed-tools 'bash(git *)'
```

Pre-approve one specific Alibaba Cloud write API:

```bash
iac-code --prompt "Deploy the stack" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Run in automation with no interactive prompts:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```

Start SDK process mode for a long-running subprocess client:

```bash
iac-code --input-format stream-json --output-format stream-json
```

In process mode the caller writes one JSON frame per line to stdin and reads one JSON frame per line from stdout. Send an `initialize` control request before user messages. This mode supports `interrupt`, `set_model`, `end_session`, `close`, `keep_alive`, and `update_environment_variables` control flows. When `IAC_CODE_MODE=pipeline`, the same process entry point runs Pipeline mode and returns pipeline status in stream and result frames.
