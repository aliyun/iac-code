---
sidebar_position: 1
title: Overview
description: What IaC Code does and where to start.
---

# Overview

IaC Code is an AI-powered assistant for planning, generating, deploying, and managing cloud infrastructure. You can use it from the Desktop app, local Web app, interactive terminal, automation interfaces, or as a Skill in another agent. The architecture is designed for multi-cloud workflows; the current release supports Alibaba Cloud ROS and Terraform workflows.

Core capabilities:

- **Say it, ship it** — describe what you need in plain language and get validated ROS templates ready to deploy, or generated Terraform templates.
- **One command to production** — for Alibaba Cloud ROS, go from template to running infrastructure in one flow: create, update, delete, and monitor stacks across regions. Terraform support covers template generation and conversion, not deployment.
- **Cloud smarts built in** — search documentation, check resource availability, and estimate costs before you deploy; every decision backed by real cloud data.

Choose the entry point that matches your workflow:

- Download the [Desktop app](./desktop-app.md) for a ready-to-use graphical application.
- Follow [Installation](./getting-started/installation.md) and [Quick Start](./getting-started/quick-start.md) to use the REPL, headless mode, or local [Web app](./web-app.md).
- Choose an option in [Official IaC Code Skills](./a2a/skill-overview.md) to give a compatible agent IaC Code's Alibaba Cloud infrastructure capabilities.
- Use [ACP](./acp/overview.md), [A2A](./a2a/overview.md), or [AG-UI](./agui/overview.md) when integrating IaC Code into another application or service.

Model configuration is required for every entry point. Configure [Alibaba Cloud credentials](./configuration/alibaba-cloud-credentials.md) when a task needs to query, change, or deploy cloud resources.
