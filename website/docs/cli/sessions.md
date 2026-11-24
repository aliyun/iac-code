---
title: Sessions
description: Persist and resume conversations across runs.
---

# Sessions

Web, Desktop, and headless runs share the session store. Conversations are saved automatically under the runtime configuration directory.

## Web and Desktop

### Continue a conversation

1. Find the project group in the sidebar and click the conversation. If necessary, use **Search** to find it by title.

2. Review the saved messages and generated files.

3. Enter a follow-up request and send it. To start an independent task, use **New chat** instead.

### Rename or pin a conversation

1. Open the conversation, then open its **…** menu.

2. Choose **Rename conversation**, enter the new title, and click **Save**. The sidebar shows the updated title.

3. To keep it easy to find, choose **Pin conversation** in the same menu. Use the unpin action to remove the pin later.

### Archive and restore a conversation

1. Open the conversation’s **…** menu and choose **Archive conversation**. It leaves the active sidebar list.

2. To restore it, open **Settings → Archived conversations**, then find it with the search and project filters.

3. Click **Unarchive** beside the conversation. Return to the sidebar and open it to continue.

:::note

Conversations created through CLI, A2A, or ACP are hidden by default. To inspect them, open **Settings → General → Foreign session visibility** and enable the relevant read-only option. Sharing the session store does not make these conversations editable in Web/Desktop.

:::

## Headless continuation

Run from the session’s project directory. Add `--resume` to continue a specific session, or `--continue` to use the latest session in that project. The short forms are `-r` and `-c`; do not combine the two options.

```bash
iac-code --resume deploy-prod --prompt "Generate a VPC template"
iac-code --continue --prompt "Generate a VPC template"
```

## Other projects

In Web or Desktop, open a conversation under its project in the sidebar. For headless runs, first change to that project’s directory:

```bash
cd /path/to/project
iac-code --resume <session-id> --prompt "Generate a VPC template"
```

## Interrupted conversations

Reopen the saved conversation to inspect its history and continue. Recovery reconciles unfinished tool calls. Pipeline progress and pending decisions are restored through the [pipeline recovery flow](../automation/pipeline-mode.md#interaction-and-recovery); one-shot `--prompt` runs do not execute pipelines.
