---
title: Legacy Terminal Interface
description: Compatibility entry point for existing terminal workflows.
---

# Legacy Terminal Interface

TUI is the legacy interface for conversations and tool interaction directly in a terminal. This page is for existing terminal workflows.

:::note

TUI is no longer updated or maintained. New interface features target [Web](../web-app.md) and [Desktop](../desktop-app.md); the terminal interface does not promise the same controls or presentation.

:::

## Start and use TUI

1. Install the [Python package](../getting-started/installation.md). Configure a [model provider](../configuration/llm-providers.md) and, when the task needs cloud access, [Alibaba Cloud credentials](../configuration/alibaba-cloud-credentials.md). Saved configuration can be reused by TUI.

2. Open an interactive terminal, change to the project directory, then start TUI:

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. After the maintenance notice and input area appear, enter your request and submit it. Responses and tool activity appear in the terminal. Answer questions and permission prompts there when required.

4. Use `/help` to inspect commands supported by the installed version, `/status` to inspect the current session, and `/exit` to leave.

TUI needs an interactive terminal. For scripts or CI without a terminal, use [headless automation](../automation/non-interactive-mode.md). Running `iac-code` without a subcommand opens Web by default.

## Continue a terminal conversation

Run from the conversation’s project directory. Resume a specific session with its ID or unique name:

```bash
iac-code tui --resume <session-id-or-name>
```

Or continue the latest session in the current project:

```bash
iac-code tui --continue
```

Do not combine `--resume` and `--continue`. Use `iac-code tui --help` to inspect startup options for your installed version.

## Move to Web or Desktop

1. Run `iac-code web`, or open the installed Desktop app, then select the same project directory.

2. With the same operating-system user and `IAC_CODE_CONFIG_DIR`, saved model configuration, cloud credentials, and instruction memory can be reused. Manage these through Settings; see [Common Actions](./commands.md) for the steps.

3. Start a new conversation for further work. To inspect an old terminal conversation, enable the relevant read-only option under **Settings → General → Foreign session visibility**. The shared session store does not make terminal conversations editable in Web/Desktop; see [Sessions](./sessions.md).
