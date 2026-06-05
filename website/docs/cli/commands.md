---
title: Slash Commands
description: Complete reference for built-in interactive commands.
---

# Slash Commands

Slash commands control IaC Code from inside an interactive session. Type `/` to see available commands, then continue typing to filter the list. A command is recognized only when it appears at the start of your message.

The `/` listing includes both built-in commands and any skills you have configured. To restrict suggestions to skills only, use `$` instead — `$<name>` lists and invokes skills exclusively, and typing `$` followed by a built-in command name (for example `$help`) prints an error pointing at the `/` equivalent.

Text after the command name is passed as arguments. In the table below, `<arg>` indicates a required argument and `[arg]` indicates an optional argument.

| Command | Purpose |
|---|---|
| `/auth` | Configure model provider access and Alibaba Cloud credentials through the interactive authentication flow. Use this when setting up IaC Code for the first time, changing API keys, switching providers, or updating cloud access. Alias: `/login`. |
| `/clear` | Clear the current conversation history and reset the active context manager. In interactive mode, it also clears the terminal screen and re-renders the welcome banner. Use it when you want to start a fresh request without leaving the REPL. |
| `/compact` | Summarize the current conversation to reduce context usage while preserving recent turns. Use it after a long session when you want to continue working with less accumulated context. If the conversation is empty or too short, the command reports that there is nothing to compact. |
| `/debug [on\|off\|status]` | Inspect or change runtime debug logging for the active session. `/debug` and `/debug status` show whether logging is enabled and, when enabled, the log file path. `/debug on` enables logging for the current session. `/debug off` disables it. |
| `/effort [level]` | Show or change thinking effort for the active model when the selected model supports effort control. With a level, it applies the requested value if valid for the model. Without a level, it opens an interactive picker in the REPL, or prints the current effort in non-interactive contexts. |
| `/exit` | Exit the interactive REPL. Aliases: `/quit`, `/q`. |
| `/help` | Show available commands and common keyboard shortcuts inside the REPL. Alias: `/?`. |
| `/memory` | Open the memory selector. Edit project or user `IAC-CODE.md` files, toggle auto-memory, and open the project auto-memory folder when auto-memory is on. |
| `/model [model_name]` | Show or switch the active model. With `model_name`, it switches directly to that model for the active provider. Without an argument, it opens an interactive model picker when a provider is configured, or prints the current model when no console UI is available. |
| `/rename <name>` | Name the current session. Names appear in the welcome banner, exit hint, and `/resume` picker, and can be used with `/resume` or `--resume` when they uniquely identify a session. |
| `/resume [session id\|unique id prefix\|unique session name]` | Resume a previous session. With an argument, IaC Code resolves it as an exact session ID, unique ID prefix, or unique session name. Without an argument, it opens the interactive session picker. Cross-project sessions print a `cd ... && iac-code --resume <id>` command instead of hot-swapping the current project. |
| `/skills` | Open the skill management picker. Search skills, sort by name/source/size, and enable or disable user and project skills. Bundled skills remain locked on. |
| `/status` | Show current session ID, provider, model, Alibaba Cloud region, working directory, recorded API token usage, turn count, and context utilization. In debug mode, it also shows memory recall side-call counts and token usage. |

The exact command list can change between releases. Use `/help` or type `/` in the REPL to inspect the commands available in your installed version.

## Memory

Use `/memory` to edit the memory files IaC Code loads into the conversation:

- Project memory is saved in `IAC-CODE.md` at the project root.
- User memory is saved in `IAC-CODE.md` in the runtime configuration directory, `~/.iac-code/` by default.
- The editor is a compact Vim-like full-screen editor. Use `i`, `a`, or `o` to enter insert mode, `Esc` to return to normal mode, `:wq` to save, and `:q!` to discard.
- The `Auto-memory` row can be toggled with `Enter`. When auto-memory is on, IaC Code can recall relevant project topic memories as hidden conversation context.
- The auto-memory folder option appears only when auto-memory is on.
