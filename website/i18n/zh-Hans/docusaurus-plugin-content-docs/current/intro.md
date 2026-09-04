---
sidebar_position: 1
title: 概览
description: IaC Code 的用途以及从哪里开始。
---

# 概览

IaC Code 是用于规划、生成、部署和管理云基础设施的 AI 助手。你可以通过桌面版、本地 Web 版、交互式终端、自动化接口使用，也可以把它作为 Skill 集成到其他 Agent 中。架构设计面向多云工作流；当前版本支持阿里云 ROS 与 Terraform 工作流。

核心能力：

- **说出来，就生成** — 用自然语言描述需求，自动生成经过校验、可直接部署的 ROS 模板，或生成 Terraform 模板。
- **一句话到上线** — 面向阿里云 ROS，从模板到基础设施运行一站式完成：创建、更新、删除资源栈，并跨地域监控部署进度；Terraform 支持仅覆盖模板生成与转换，不包含部署。
- **云端智能加持** — 搜索云产品文档、查询资源库存、部署前估算成本；每一个决策都有真实云数据支撑。

根据使用方式选择入口：

- 下载[桌面版](./desktop-app.md)，直接使用图形化应用。
- 阅读[安装](./getting-started/installation.md)和[快速开始](./getting-started/quick-start.md)，使用 REPL、无头模式或本地 [Web 版](./web-app.md)。
- 通过 [IaC Code 官方 Skills 概览](./a2a/skill-overview.md)选择合适的发行版，让兼容的 Agent 获得 IaC Code 的阿里云基础设施能力。
- 通过 [ACP](./acp/overview.md)、[A2A](./a2a/overview.md) 或 [AG-UI](./agui/overview.md) 将 IaC Code 集成到其他应用或服务。

所有入口都需要配置模型。任务需要查询、变更或部署云资源时，还需要配置[阿里云凭证](./configuration/alibaba-cloud-credentials.md)。
