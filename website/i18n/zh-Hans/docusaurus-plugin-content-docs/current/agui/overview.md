---
sidebar_position: 1
title: AG-UI 协议
description: iac-code AG-UI 接入的架构、能力和适用场景。
---

# AG-UI 协议

## 什么是 AG-UI

[Agent-User Interaction Protocol（AG-UI）](https://docs.ag-ui.com/concepts/architecture) 是 agent 与用户侧应用之间的事件流协议。客户端通过 `RunAgentInput` 发起一次运行，并通过 HTTP Server-Sent Events（SSE）接收文本、推理、工具调用、步骤、状态和 Interrupt 等结构化事件。

AG-UI 适合 Web 控制台、聊天客户端、IDE 插件和其他需要实时展示 agent 执行过程的应用。与只消费最终文本相比，AG-UI 客户端可以分别渲染模型回答、工具参数、工具结果、Pipeline 步骤和待用户确认的操作。

## iac-code 的实现架构

iac-code 使用 **A2A 执行内核 + AG-UI 协议适配器**：

```text
AG-UI client
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Agent loop / Pipeline / LLM / Alibaba Cloud API
```

`iac-code a2a` 是唯一执行内核，负责：

- normal 会话和 Pipeline 执行；
- iac-code session、A2A context 和 task；
- 工具权限、提问、方案选择和恢复；
- 执行生命周期和取消；
- LLM 与 Alibaba Cloud API 调用。

`iac-code agui` 不创建第二套 Agent runtime，也不直接运行 Pipeline。它只负责：

- 将 AG-UI `RunAgentInput` 转换为 A2A 请求；
- 将 A2A 事件投影为标准 AG-UI 事件；
- 映射 `threadId/runId` 与 A2A `contextId/taskId`；
- 将 AG-UI `resume[]` 转换为 A2A 输入恢复；
- 持久化协议映射和待处理 Interrupt；
- 将取消请求转发给 A2A。

因此，AG-UI 与 A2A 不会各自维护一套执行语义。模型选择、云凭据、权限规则和 Pipeline 行为最终都由同一个 A2A runtime 处理。

## 标准协议与 iac-code 扩展

对外事件流使用标准 AG-UI 事件，包括：

- `RUN_STARTED`、`RUN_FINISHED` 和 `RUN_ERROR`；
- `TEXT_MESSAGE_*`；
- `REASONING_*`；
- `TOOL_CALL_*`；
- `STEP_STARTED` 和 `STEP_FINISHED`；
- `ACTIVITY_SNAPSHOT`。

只有标准事件无法表达、且客户端确实需要展示的 iac-code Pipeline 信息才使用命名空间明确的 `CUSTOM` 事件。普通 AG-UI 客户端可以忽略这些 `CUSTOM` 事件，不影响文本、工具调用、Interrupt 和 run 生命周期。

请求仍是标准 `RunAgentInput`。iac-code 通过标准的 `forwardedProps` 承载工作区、运行模式和临时凭据等服务端必需信息：

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "request-identity",
      "cwd": "/absolute/workspace/path",
      "runMode": "normal"
    }
  }
}
```

这意味着通用 AG-UI client 可以直接消费 iac-code 的标准事件，但直接调用 `iac-code agui` 时，仍需在 `forwardedProps.iacCode` 中提供 `cwd` 等运行参数。

## 支持的交互

### 多轮 normal 会话

同一会话持续使用相同 `threadId`，每个用户 turn 使用新的 `runId`。AG-UI adapter 将其绑定到同一个 iac-code session；一个 turn 完成后，下一条用户消息通过新的 HTTP/SSE 请求开始，不会继续使用上一个已经结束的 SSE 响应。

### Pipeline

设置 `forwardedProps.iacCode.runMode` 为 `pipeline` 后，执行仍由 A2A Pipeline 内核完成。顶层步骤映射为标准 `STEP_*`，agent 文本、推理和工具调用映射为对应的标准事件；没有标准等价物的候选方案、资源栈进度和清理进度通过 `iac-code.pipeline.v1` 自定义事件输出。

Pipeline 中的并行 sub-pipeline 使用不同的消息和步骤标识，不会把多个 agent loop 的文本合并成同一条消息。

### Interrupt 与 Resume

当权限申请、提问或方案选择需要用户输入时，当前 run 会以以下事件结束：

```json
{
  "type": "RUN_FINISHED",
  "outcome": {
    "type": "interrupt",
    "interrupts": []
  }
}
```

Interrupt 已持久化后才会向客户端发布。客户端收集响应，再以同一 `threadId`、新的 `runId` 和 `resume[]` 发起新请求。Resume 的 SSE 连接属于这个新请求，不会重新接回旧连接。

### Adapter 状态

AG-UI adapter 按 thread 分文件保存协议映射、幂等信息和待处理 Interrupt。该目录不保存对话正文、LLM key 或云凭据，也不是会话导出目录。

## 何时使用 AG-UI

| 需求 | 推荐模式 |
|------|----------|
| 构建聊天界面并实时展示文本、推理、工具和步骤 | **AG-UI** |
| 在 UI 中处理权限、提问和方案选择 | **AG-UI** |
| 另一个 agent 或编排系统直接调用 iac-code | **A2A** |
| IDE/编辑器使用 ACP session 和终端能力 | **ACP** |
| 本地人工操作 | **交互式 REPL 或 Web/Desktop** |

AG-UI 和 A2A 可以同时启动。对外提供两种协议时，它们仍共享 iac-code 的执行实现，而不是共享同一个 HTTP 端点。

## 当前边界

- AG-UI 传输为 HTTP POST + SSE。
- A2A upstream 必须是本机回环地址；AG-UI adapter 不允许连接任意远程 A2A URL。
- `cwd` 必须按请求传入，并且必须位于允许的工作区根目录内。
- 当前不接受 client 自定义的 `tools`；工具集合由 iac-code runtime 管理。
- 用户消息支持文本和内联 base64 图片；不接收远程媒体 URL。
- 客户端主动断开一个仍在运行、尚未进入 Interrupt 的 SSE 时，adapter 会取消对应 A2A task。
- SSE 每 15 秒发送注释形式的 heartbeat；符合规范的 SSE 客户端会忽略它。

## 后续阅读

- [快速开始](./getting-started.md) — 安装、启动和连接第一个客户端。
- [协议参考](./protocol-reference.md) — 请求字段、事件、Interrupt/Resume、持久化和错误语义。
