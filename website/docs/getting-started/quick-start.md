---
title: Quick Start
description: Start Desktop or Web, configure access, and send your first request.
---

# Quick Start

The steps below apply to both Desktop and Web; they use the same conversation interface.

1. Open [Desktop](../desktop-app.md), or run `iac-code` in an interactive terminal to open Web.

2. Click **New chat**. In the project control below the input, select where the template should be saved.

3. Follow [model setup](../configuration/llm-providers.md) to save a provider and set the current model. For cloud queries or deployment, also configure [cloud credentials](../configuration/alibaba-cloud-credentials.md).

4. Return to the input, choose **Normal mode**, and send: “Generate a VPC ROS template, save it to templates/vpc.yml, and do not deploy it yet.”

5. Watch the processing entries in the conversation. If Wiki search runs, expand its entry to see the keywords and matched content. Review any file-write approval card and answer it.

6. When the assistant writes the template, click its filename in **Output** to preview it. Ask a follow-up to explain or validate its parameters. The conversation is saved in the sidebar.

For guided planning and deployment, create a new conversation and choose [Pipeline mode](../automation/pipeline-mode.md). See [Common Actions](../cli/commands.md) for daily controls and [headless automation](../automation/non-interactive-mode.md) for scripts.
