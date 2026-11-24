---
sidebar_position: 2
title: MCP 快速开始
description: 通过桌面版或 Web 的插件面板添加并连接 MCP 服务器。
---

# MCP 快速开始

桌面版和 Web 通过**插件**中的 **MCP** 标签页添加、管理服务器。下面也保留 CLI 命令和配置文件说明，供自动化和高级配置使用。

## 连接远程 HTTP 服务器

1. 打开**插件 → MCP**，选择目标项目，单击**新增服务器**。

2. 填写服务器**名称**，选择**作用域**，将**传输类型**设为 **http（远程）**。

3. 填写服务器 **URL**。需要请求头时补充配置，敏感值使用环境变量引用。

4. 单击**创建**。如果项目级服务器显示待审批，检查配置后单击**批准**。

5. 如果需要认证，单击**认证**并在浏览器完成授权，成功后返回面板。

6. 单击**检查连接**，再单击**查看能力**；确认预期工具、资源或提示词已发现后，在对话中使用。

## 连接本地 stdio 服务器

1. 打开**插件 → MCP**，选择项目并单击**新增服务器**。

2. 填写**名称**和**作用域**，选择 **stdio（本地命令）**。

3. 填写**命令**，在**参数**中每行填写一个参数，并填写所需环境变量。可执行命令须位于运行 IaC Code 的机器上。

4. 单击**创建**；项目级配置需要审批时，检查后批准。

5. 单击**检查连接**和**查看能力**，确认连接与能力发现成功。

:::note

用户作用域跨项目使用；本地作用域仅用于所选检出目录；项目作用域写入可共享的 `.mcp.json`，需要本地审批。修改已有服务器时，使用编辑操作并单击**保存**。

:::

## CLI 自动化

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## 下一步

- [MCP 配置](./configuration.md)：作用域、项目文件、请求头、OAuth 选项和高级 JSON 配置。
- [OAuth 和安全](./oauth-and-security.md)：令牌存储、认证重置及审批行为。
- [疑难解答](./troubleshooting.md)：处理待审批、需要认证或连接失败等问题。
