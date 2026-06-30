---
title: Command Line Options
description: Reference for IaC Code startup options and one-shot execution flags.
---

# Command Line Options

Command line options change how IaC Code starts. Use them before entering the interactive REPL, or combine them with `--prompt` for one-shot automation.

| Option | Purpose |
|---|---|
| `-h`, `--help` | Show CLI help and exit. Use this to inspect the options supported by your installed version. |
| `-v`, `-V`, `--version` | Print the installed IaC Code version and exit. |
| `-m <model>`, `--model <model>` | Start with a specific LLM model. This overrides the saved model for the current run. |
| `-p <prompt>`, `--prompt <prompt>` | Run a single prompt and exit. This enables non-interactive mode. Use `--prompt -` to read the prompt from standard input. |
| `--output-format <format>` | Set output format for non-interactive mode. Supported values are `text`, `json`, and `stream-json`. The default is `text`. |
| `--max-turns <number>` | Limit the maximum number of agent turns in non-interactive mode. The default is `100`. |
| `-d`, `--debug` | Enable debug logging for the current run. In interactive mode, use `/debug` to inspect or change debug logging after startup. |
| `-r <session-id-or-name>`, `--resume <session-id-or-name>` | Resume a previous session by exact session ID, unique ID prefix, or unique session name. Cross-project resolved sessions print a `cd ... && iac-code --resume <id>` command instead of hot-swapping the current project. |
| `-c`, `--continue` | Resume the most recent session. This cannot be used together with `--resume`. |
| `--allowed-tools <patterns>` | Comma-separated tool permission patterns to allow, e.g. `'bash(git *),write_file'`. |
| `--disallowed-tools <patterns>` | Comma-separated tool permission patterns to deny, e.g. `'bash(rm *)'`. |
| `--permission-mode <mode>` | Permission mode: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |

## Permission Modes

The `--permission-mode` flag controls how the agent handles tool permission checks:

| Mode | Behavior |
|---|---|
| `default` | The agent prompts for confirmation when a tool action requires approval. |
| `accept_edits` | Auto-approve file system commands that are considered edits (e.g. `mkdir`, `cp`). Other actions still prompt. |
| `bypass_permissions` | Auto-approve tool actions except safety checks. Any allow decision that requires an audit record fails closed if audit persistence fails. Intended for trusted automation. |
| `dont_ask` | Silently deny any action that would normally prompt. Useful for strict read-only runs. |

Alibaba Cloud write API calls made through `aliyun_api` are protected separately: a bare `aliyun_api` allow rule does not blanket-approve them, and outside `bypass_permissions` write allow rules must match the exact canonical `product:action` pair. `bypass_permissions` does auto-approve them, but like other audited allow decisions, the action is denied if the permission audit record cannot be persisted. Use exact rules such as `aliyun_api(ros:CreateStack)` when trusted automation should allow only a specific write API.

## Common Startup Commands

Start the interactive REPL with the saved model:

```bash
iac-code
```

Start with a specific model for this run:

```bash
iac-code --model qwen3.6-plus
```

Run a one-shot prompt:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Read the prompt from standard input:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Resume the latest session:

```bash
iac-code --continue
```

Allow only git and read-only bash commands:

```bash
iac-code --allowed-tools 'bash(git *)'
```

Allow one specific Alibaba Cloud write API:

```bash
iac-code --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Run in automation with no interactive prompts:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
