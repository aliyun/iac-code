---
title: Common Actions
description: Use the settings, conversation controls, and composer in Desktop and Web.
---

# Common Actions

The steps below apply to both Desktop and Web; they use the same conversation interface.

## Start a conversation

1. Open Desktop, or run `iac-code` in an interactive terminal to open Web.

2. Click **New chat** in the sidebar. In the project control below the composer, select the directory that contains your templates or should receive generated files.

3. Before sending the first message, select **Normal mode** or **Pipeline mode**. For Pipeline, also choose the pipeline to run.

4. Enter the task and send it. The conversation appears under its project in the sidebar; follow-up messages belong to the same conversation.

## Use an existing file or an image

1. Open the conversation for the project containing the file.

2. Type `@` in the composer, enter part of the workspace file name, and select the matching item. A file reference appears beside the input. For an image, use the attachment control and select the image.

3. Add the instruction, for example “Check the referenced ROS template and explain the parameters,” then send. Review the selected references before sending.

## Choose a model and thinking settings

1. Configure the provider first using the [model setup steps](../configuration/llm-providers.md).

2. Return to the conversation and open the model control below the composer. Choose an available provider and model.

3. Choose a reasoning effort when the model supports it, and use **Thinking** to enable or disable thinking for this conversation.

4. Confirm the model shown below the input, then send the next request. Available thinking options depend on the model.

## Handle a tool approval

1. Before sending the request, open the permission control in the composer and choose the policy you want.

2. When an approval card appears in the conversation, read the tool name and proposed operation, including the target file or cloud API.

3. Use the card’s approval or rejection controls. Approval allows the pending operation to continue; rejection returns the decision to the assistant. Answer question cards separately when more information is needed.

## Edit instructions and manage memory

1. Open **Settings → Memory** and select the target project at the top.

2. Under **Project memory**, enter instructions that apply to that project and click its **Save** button.

3. Under **User memory**, enter preferences that apply across projects and click that section’s **Save** button. Wait for the saved status after each edit.

4. To let the assistant record useful facts while working, switch **Automatic memory → Auto-record** on or off. This switch saves automatically.

5. In **Memory library**, search for recorded entries and inspect their content. Use an entry’s delete control and confirm it to remove an obsolete entry.

## Change language or appearance

1. Open **Settings → General**.

2. Choose a value under **Language**. It is saved automatically and the page reloads in the selected language.

3. Under **Color scheme**, click a theme. The interface updates and saves the choice automatically.

## Other tasks

For detailed steps, see [Sessions](./sessions.md), [Skills](./skills.md), and [MCP Quick Start](../mcp/quick-start.md).

## Optional input shortcuts

In an existing normal conversation, typing `/` exposes a small set of shortcuts:

| Shortcut | Purpose |
|---|---|
| `/status` | Show the current session status. |
| `/compact` | Summarize the conversation to reduce context usage. |
| `/mcp` | Show MCP server status; manage connections in **Plugins → MCP**. |

Pipeline conversations omit `/compact`; these session shortcuts are absent from a new-conversation draft. Skills also appear in the suggestions: select one, add your request, and send. See [Skills](./skills.md) for details.
