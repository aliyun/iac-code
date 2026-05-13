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
| `-r <session-id>`, `--resume <session-id>` | Resume a previous session by ID. This is for returning to a known conversation. |
| `-c`, `--continue` | Resume the most recent session. This cannot be used together with `--resume`. |

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
