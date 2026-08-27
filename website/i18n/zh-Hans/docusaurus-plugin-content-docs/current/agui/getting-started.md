---
sidebar_position: 2
title: 快速开始
description: 安装、启动并调用 iac-code AG-UI adapter。
---

# AG-UI 快速开始

## 前提条件

1. 已安装 Python 3.10 或更高版本。
2. 已配置 iac-code 使用的 LLM provider。请参阅[认证](../configuration/authentication.md)。
3. 如果任务需要访问 Alibaba Cloud，已配置云凭据，或由调用方按请求传入临时凭据。
4. 已准备一个允许 iac-code 读写的工作区绝对路径。

安装 AG-UI 依赖：

```bash
pip install "iac-code[agui]"
```

在源码仓库开发时使用：

```bash
uv sync --extra agui
```

## 方式一：自动启动本地 A2A 内核

最简单的本地启动方式是省略 `--a2a-url`：

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

AG-UI adapter 会自动选择空闲回环端口，启动受管的 `iac-code a2a` 子进程，并在退出时关闭它。子进程继承当前 iac-code 配置和运行环境。

这种方式适合本地开发和单进程管理。生产环境需要分别管理两个进程时，请使用下一种方式。

## 方式二：连接独立 A2A 内核

先启动 A2A server：

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

再启动 AG-UI adapter：

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

两个服务的职责和端口相互独立：A2A 仍可继续对外提供 A2A 接口，AG-UI adapter 只通过回环地址调用它。

`--thinking-exposure all` 让 adapter 能把 raw thinking 转成标准 `REASONING_*` 事件。只有受信任的客户端才应启用 raw thinking；不需要展示推理时可以沿用 A2A 默认的 `tool-trace`。

如果 A2A server 启用了 Bearer token：

```bash
export IACCODE_A2A_HTTP_TOKEN="a2a-local-secret"
iac-code a2a --host 127.0.0.1 --port 41242
```

AG-UI 进程需使用同一个 upstream token：

```bash
export IAC_CODE_AGUI_A2A_TOKEN="a2a-local-secret"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## 使用 YAML 配置

静态启动参数可以写入 YAML：

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

启动：

```bash
iac-code agui --config agui-server.yml
```

命令行显式参数优先于 YAML。token 等敏感值建议通过环境变量注入，而不是写入配置文件。

常用参数：

| CLI / YAML | 默认值 | 含义 |
|------------|--------|------|
| `--host` / `host` | `127.0.0.1` | AG-UI HTTP 监听地址 |
| `--port` / `port` | `8000` | AG-UI HTTP 端口；部署示例使用 `41243` |
| `--a2a-url` / `a2a-url` | 空 | 本地 A2A URL；为空时启动受管子进程 |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Interrupt 可恢复秒数 |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | AG-UI thread 状态目录 |
| `--idle-shutdown` / `idle-shutdown` | `0` | 空闲自动退出秒数；`0` 表示关闭 |
| `--debug` / `debug` | `false` | 调试日志 |
| `--log-stdout` / `log-stdout` | `false` | 同时向标准输出写日志 |

相关环境变量：

| 环境变量 | 用途 |
|------------|------|
| `IAC_CODE_AGUI_HOST` | AG-UI 监听地址 |
| `IAC_CODE_AGUI_PORT` | AG-UI 监听端口 |
| `IAC_CODE_AGUI_A2A_URL` | 本地 A2A upstream URL |
| `IAC_CODE_AGUI_A2A_TOKEN` | A2A upstream Bearer token |
| `IAC_CODE_AGUI_AUTH_TOKEN` | 保护 AG-UI endpoint 的 Bearer token |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Interrupt 有效期 |
| `IAC_CODE_AGUI_STATE_DIR` | AG-UI thread 状态目录 |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | 允许的工作区根目录，使用操作系统路径分隔符分隔 |
| `IAC_CODE_CONFIG_DIR` | iac-code 配置根目录，也决定默认 AG-UI 状态目录 |

## 健康检查

```bash
curl http://127.0.0.1:41243/health
```

响应示例：

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "当前 iac-code 版本"
}
```

## 使用官方 JavaScript client

安装已验证的客户端版本：

```bash
pnpm add @ag-ui/client@0.0.58
```

下面的示例直接连接 `iac-code agui`。它使用标准 `HttpAgent`，并在 `forwardedProps` 中提供 iac-code 运行参数：

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const threadId = randomUUID();
const rosInvocationId = randomUUID();
const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId,
  // 如果设置了 IAC_CODE_AGUI_AUTH_TOKEN：
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId,
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "zh-CN",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "创建一个包含两个交换机的 VPC 模板。",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[tool] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[step] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

如果 endpoint 设置了 Bearer token，使用 `HttpAgent.headers` 传入 `Authorization`。

浏览器页面通常需要通过同源后端或反向代理连接 AG-UI endpoint；当前 adapter 不负责添加跨域策略。

## 处理 Interrupt

官方 client 会把 `RUN_FINISHED.outcome.interrupts` 维护在 `agent.pendingInterrupts` 中。必须根据每个 Interrupt 的 `responseSchema` 构造响应，并在新 run 中提交：

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent(
  {
    forwardedProps,
    resume: responses,
  },
  subscriber,
);
```

上例只适用于 `responseSchema` 要求 `decision` 的权限 Interrupt。提问和方案选择必须按各自 schema 提交，不要假设所有 Interrupt 都有相同结构。

Resume 必须满足：

- 使用原来的 `threadId`；
- 使用新的 `runId`（官方 client 默认生成）；
- `rosInvocationId` 与被中断的 execution 保持一致；
- 一次覆盖当前所有 pending Interrupt；
- `status` 为 `resolved` 时提供符合 `responseSchema` 的 `payload`；
- 不想继续时可发送 `status: "cancelled"`。

## 启动 Pipeline

将 `runMode` 改为 `pipeline`，并按需指定 Pipeline 名称：

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

客户端应同时处理 `STEP_*`、`TOOL_CALL_*`、`ACTIVITY_SNAPSHOT` 和 `CUSTOM`。不认识 iac-code 自定义事件的通用客户端仍可正常处理标准事件。

## 工作区与临时凭据

`cwd` 不在服务启动时固定，而是每个请求都必须提供。它必须是绝对路径，并位于 `IAC_CODE_AGUI_ALLOWED_CWDS` 或 `IACCODE_A2A_ALLOWED_CWDS` 允许的根目录内。

调用方可以通过 `forwardedProps.iacCode` 传入单次请求的模型、LLM key 和 Alibaba Cloud 临时凭据。AG-UI adapter 不把这些 secret 写入自己的 thread 状态文件；它们会被转发给 A2A 执行内核，并按 A2A 的请求级覆盖规则使用。

## 持久化目录

默认状态结构为：

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

每个 thread 独立写入，不会在进程启动时扫描全部历史 thread。普通 UUID 保持可读文件名；无法安全作为文件名的 ID 会被编码，超长 ID 使用固定长度文件键，JSON 内容始终保存并校验原始 `threadId`。

该目录只保存 AG-UI adapter 的 thread 映射、Interrupt 和幂等状态，不保存对话正文或请求中的凭据。不要手工编辑其中的 JSON 文件。

## 下一步

- [AG-UI 协议概览](./overview.md)
- [协议参考](./protocol-reference.md)
