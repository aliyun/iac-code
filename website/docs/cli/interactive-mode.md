---
title: Interactive Mode
description: Use the REPL for iterative infrastructure work.
---

# Interactive Mode

Run without arguments to enter the interactive REPL:

```bash
iac-code
```

Interactive mode is useful when you want to refine infrastructure requirements over multiple turns.

Start with authentication:

```text
/auth
```

Then describe what you want to build:

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```

## Commands

Type `/` to discover available slash commands. Common operational commands include `/status` for the current session state, `/skills` for skill management, `/memory` for project and user memory files, `/rename` for naming the active session, and `/resume` for switching sessions.

Type `$` to discover and invoke skills only.

## Editing input

Use `Shift+Enter` to insert a newline without sending the prompt. Press `Enter`
on its own to submit the complete prompt.

If your terminal does not report `Shift+Enter` separately, press `Esc` and then
`Enter` to insert a newline. Multi-line prompts are saved as one history entry,
so pressing `Up` restores the full prompt.

## Shell escapes

Prefix a line with `!` to run a local shell command from the REPL through the
built-in `bash` tool:

```text
!pwd
!git status --short
```

IaC Code applies the normal tool permission checks, runs the command in the
current project context, and prints the output in the terminal. The command is
not sent to the model as a chat message.
