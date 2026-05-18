---
sidebar_position: 1
title: A2A 协议
description: iac-code 中 Agent2Agent 支持的概览。
---

# A2A 协议

## 什么是 A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) 是一种用于发现和调用远程 agent 的协议。它允许 agent 发布 Agent Card、接收结构化消息、流式传输任务更新，并通过标准传输暴露取消和任务查询操作。

## iac-code 作为 A2A 服务器

iac-code 可以作为 A2A 1.0 Server / Agent 运行。其他兼容 A2A 的客户端可以发现它、发送 Infrastructure as Code 请求、流式接收执行更新，并取消活动任务。

当另一个 agent、工作流引擎或服务需要把 iac-code 作为可互操作的 IaC 专家来调用时，请使用 A2A。当编辑器风格的客户端需要会话管理、权限提示和本地开发集成时，请使用 ACP。

## 使用场景

- **Agent 编排** — 规划 agent 可以把 Alibaba Cloud ROS 或 Terraform 工作委派给 iac-code。
- **工作流自动化** — 内部工具可以通过 HTTP 提交 IaC 生成、审查或转换任务。
- **服务发现** — 客户端可以获取 Agent Card，并选择 IaC 生成或模板审查等能力。
- **流式集成** — chatops 或仪表板客户端可以在轮次运行时显示模型文本、工具活动、用量元数据和最终任务状态。

## 交互模式对比

| 模式 | 命令 | 最适合 |
|------|---------|----------|
| **交互式 REPL** | `iac-code` | 上手探索和迭代式模板编写 |
| **非交互式 CLI** | `iac-code --prompt "..."` 或 `--headless` | 一次性脚本和 CI 作业 |
| **ACP Server** | `iac-code acp` | IDE/编辑器集成和多会话客户端控制 |
| **A2A Server** | `iac-code a2a` | 通过 A2A 传输实现 agent 到 agent 的互操作 |
| **A2A Client** | `iac-code a2a-client call` | 从 iac-code 调用远程 A2A agent |

## 核心能力

- **Agent Card 发现** — 发布 `/.well-known/agent-card.json`，包含协议绑定、版本、技能、输入/输出模式和可选认证元数据。
- **HTTP JSON-RPC 和 REST** — 在 `/` 提供 A2A JSON-RPC 请求服务，并注册 SDK REST 路由。
- **流式响应** — 支持 `SendStreamingMessage`，用于增量任务更新。
- **任务管理** — 支持任务查询、带游标分页的认证任务列表、活动任务取消和活动任务订阅。
- **上下文复用** — 对同一 A2A `contextId` 中的后续消息复用 iac-code 运行时。
- **工作区范围限定** — 从 `iac_code.cwd` 处的消息元数据读取项目目录。
- **工具元数据** — 发出 iac-code 专用元数据，用于工具启动、输入增量、已完成工具结果、权限决策和 token 用量。
- **输入 parts** — 接收类文本 parts、JSON 数据 parts、原始 UTF-8 文本、本地工作区 `file://` 文本文件，以及表示为 prompt manifests 的有界多模态附件。
- **客户端调用** — 发现远程 Agent Cards，在配置后验证签名，并向远程 agent 发送文本提示。
- **路由** — 按显式名称、技能或 prompt/tag 匹配选择已配置的远程 agent。
- **持久化元数据** — 将本地 A2A 任务/上下文快照镜像到 JSON 文件，用作跨进程恢复元数据。
- **Artifacts** — 在流式事件正文之外存储受支持的本地文本 artifact payload，发出标准 `TaskArtifactUpdateEvent` 事件，并记录任务 `artifacts`。
- **扩展和缓存** — 公布可选的 iac-code artifact 元数据扩展，校验必需的 `A2A-Extensions`，并使用缓存标头提供 Agent Cards。
- **推送通知** — 当配置 `push-notifications: true` 时，支持 A2A 任务推送通知配置方法，并通过本地文件或 Redis 后端的投递队列进行投递。
- **Agent Card 签名** — 为 Agent Cards 添加可选的 A2A SDK JWS 签名，并支持使用已配置密钥、本地 octet JWKS 数据或远程 JWKS URL 进行基于 `kid` 的验证。
- **多种传输** — 通过 HTTP、stdio、Unix sockets、WebSocket、官方 gRPC、自定义 gRPC JSON-RPC 和 Redis Streams 传输运行。
- **CLI 操作** — 提供发现、消息发送、任务查询/列表/取消/订阅、推送配置 CRUD、扩展卡和路由预览命令。

## Phase 1 支持

iac-code 支持通过 HTTP JSON-RPC/REST 以及若干可选传输运行 A2A server 模式，并支持用于调用远程 A2A agent 的 Phase 1 client 模式。它可以发现远程 Agent Cards、选择已公布的端点、发送 A2A 1.0 prompts、查询/列出/取消/订阅任务、路由到已配置的 agent、持久化本地任务/上下文恢复元数据、将本地 artifact payload 作为标准任务 artifacts 存储、校验必需扩展、管理推送通知配置，并使用 HMAC 或 JWKS 元数据签名或验证 Agent Cards。

## Phase 1 不支持 {#phase-1-unsupported}

- stdio、Unix sockets、WebSocket、gRPC JSON-RPC envelope 和 Redis Streams 是实验性的自定义 JSON-RPC 传输。
- 官方 gRPC 需要可选依赖，并且默认使用不安全的本地服务器绑定。
- 没有分布式或共享任务存储。持久化是 iac-code 运行时配置区域下的本地文件存储。
- 进程重启后不会恢复正在运行的 asyncio 任务。
- 不会自动在后台继续已中断的远程任务。
- 没有 OSS、S3、数据库或外部对象存储 artifact 后端。
- 没有远程 HTTP URL 摄取、大型二进制分块或可恢复上传协议。本地 file URL parts 必须留在允许的工作区根目录内。
- 默认不会因为未签名的 Agent Cards 而硬失败。
- 服务器端不支持非对称 Agent Card 签名，也不会自动轮换签名密钥。
- 没有自主 planner DAG 或复杂多 agent 编排。
- Redis 后端队列的推送投递是至少一次；callback 接收方必须处理重复投递，并执行自己的端点侧授权策略。

在 A2A server 模式下，工具权限请求会被自动拒绝。只应在受信任的本地环境中运行未认证的 A2A 模式，或者使用 Bearer token、Basic auth 或 API key authentication 进行保护。
