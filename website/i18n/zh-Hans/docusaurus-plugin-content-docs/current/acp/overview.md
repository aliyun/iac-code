---
sidebar_position: 1
title: ACP 协议
description: iac-code 中 Agent Client Protocol 支持概述。
---

# ACP 协议

## 什么是 ACP

[Agent Client Protocol（ACP）](https://agentclientprotocol.com/get-started/introduction) 是 AI 代理与客户端之间的标准化通信协议。它定义了客户端（IDE、编辑器、自动化工具）如何通过结构化的 JSON-RPC 消息来启动、交互和管理代理会话。

## iac-code 作为 ACP Server

iac-code 通过 ACP Server 对外暴露其基础设施即代码能力。任何兼容 ACP 的客户端都可以将 `iac-code acp` 作为子进程启动（或通过 HTTP+SSE 连接），并以编程方式：

- 创建绑定到项目目录的会话
- 发送自然语言提示并接收流式响应
- 批准或拒绝文件写入及破坏性操作
- 管理多个并发会话

这使得 iac-code 从一个终端工具转变为适用于任何开发环境的**可组合后端**。

## 使用场景

- **IDE / 编辑器集成** — Zed、VS Code 或自定义编辑器可以将 iac-code 作为上下文服务器嵌入，在编辑器内提供 IaC 生成能力。
- **Agent 间协作** — 其他 AI 代理可以通过协议调用 iac-code 的 IaC 能力，实现多代理工作流。
- **自动化流水线** — CI/CD 脚本或 ChatOps 机器人可以无头模式调用 iac-code 来生成和验证模板。

## 交互模式对比

| 模式 | 命令 | 适用场景 |
|------|------|----------|
| **交互式 REPL** | `iac-code` | 动手探索、迭代式模板编写 |
| **非交互式 CLI** | `iac-code --prompt "..."` 或 `--headless` | 脚本化、一次性生成、CI 流水线 |
| **ACP Server** | `iac-code acp` | IDE 集成、多会话管理、编程式访问 |

ACP Server 模式是唯一支持多并发会话的模式，并且提供结构化的流式事件（工具调用、权限请求、思考过程），而非纯文本输出。

## 核心能力

- **多会话管理** — 创建、列举、分支、恢复和关闭独立会话，每个会话拥有独立的对话历史和工作目录。
- **流式响应** — 实时推送代理文本、思考过程、工具调用、工具进度和完成事件。
- **权限框架** — 只读工具自动放行；写入和破坏性工具需要客户端明确批准后才能执行。
- **双传输模式** — Stdio 用于本地/子进程场景，HTTP+SSE 用于远程和网络场景。
- **MCP Server 配置透传** — 客户端可在创建会话时声明 MCP Server 以扩展工具能力。
- **Slash 命令支持** — 通过协议转发 Slash 命令（`/compact`、`/clear`、`/debug` 等）。
- **运行时指标** — 会话级别的 Token 用量、延迟和工具调用统计。
