---
sidebar_position: 1
title: Official IaC Code Skills
description: Compare the official IaC Code Skills and choose the right installation for your workflow.
---

# Official IaC Code Skills

IaC Code is available as three official Skill distributions. They share the goal of managing Alibaba Cloud
infrastructure through an agent conversation, but differ in distribution channel and where the IaC Code agent runs.

## Choose a Skill

| Skill | Where it runs | Choose it when |
|---|---|---|
| `iac-code` | A verified IaC Code Runtime downloaded to your machine | You want the standalone package published with the iac-code project and direct control over installation and updates. |
| `alibabacloud-iac-code` | The same local, verified IaC Code Runtime, packaged for the Alibaba Cloud Agent Skills Portal | You install and update Alibaba Cloud Skills through the portal or the `npx skills` workflow. |
| `alibabacloud-ros-agent` | The hosted Alibaba Cloud ROS Agent, called through the ROS StartChat API | You want a remote ROS Agent conversation without downloading the local IaC Code Runtime. |

`iac-code` and `alibabacloud-iac-code` provide the same runtime-backed IaC Code capability. Select one distribution
for a given agent scope; installing both adds overlapping routing without adding functionality.

`alibabacloud-ros-agent` is a separate remote-service integration. It can coexist with one local Runtime distribution
when users need to choose explicitly between local IaC Code and the hosted ROS Agent.

## Get the Standalone Skill

Download the fixed stable package:

[Download iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

This distribution is best when you want to install the Skill directory yourself. It downloads the Runtime on first use
and reuses the model and Alibaba Cloud configuration under `~/.iac-code/`. See
[Install and Use the IaC Code Skill](./skill-integration.md) for supported hosts and configuration.

## Get the Alibaba Cloud Portal Skills

Find the Skills by their exact names in the
[Alibaba Cloud Agent Skills Portal](https://skills.aliyun.com/), or install them from the official repository:

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

You can also download the packages directly:

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) ·
  [source](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) ·
  [source](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

The `npx skills` installer can select a supported agent and installation scope interactively. Node.js 18 or later is
required for this installation method. If you download a ZIP, extract its top-level Skill directory into the user- or
project-level Skill directory supported by your agent and restart the agent when necessary.

## Capability and Configuration Differences

Both local Runtime distributions support normal conversations and Pipeline workflows, including architecture planning,
ROS and Terraform template work, cost estimation, stack operations, deployment, questions, candidate selection, and
permission or deployment confirmation. They require a configured model; Alibaba Cloud credentials are required when a
task reads or changes cloud resources.

The hosted `alibabacloud-ros-agent` sends conversations to the Alibaba Cloud ROS Agent through `ros:StartChat`. It uses
the Alibaba Cloud identity available to the host and does not require the local IaC Code Runtime or a locally configured
model provider. Grant only the required RAM permissions. Explicit remote cancellation additionally uses `ros:StopChat`.

Regardless of distribution, review the target resources, region, impact, price, and requested permissions before
approving a change or deployment. Do not place credentials in `SKILL.md`, prompts, or project files.

## Related Documentation

- [Install and Use the IaC Code Skill](./skill-integration.md)
- [IaC Code Skill Host Integration Reference](./skill-host-integration.md)
- [Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md)
- [A2A Protocol Overview](./overview.md)
