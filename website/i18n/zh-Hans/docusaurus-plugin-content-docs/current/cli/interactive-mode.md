---
title: 旧终端入口
description: 为已有终端工作流保留的兼容入口。
---

# 旧终端入口

TUI 是直接在终端中完成对话和工具交互的旧界面。本页供仍需使用已有终端工作流的用户查阅。

:::note

TUI 已停止更新和维护。新的界面能力面向 [Web 版](../web-app.md)和[桌面版](../desktop-app.md)，终端界面不保证具有相同的控件和展示效果。

:::

## 启动和基本使用

1. 安装 [Python 软件包](../getting-started/installation.md)，按文档配置[模型提供商](../configuration/llm-providers.md)；任务需要访问云资源时，再配置[阿里云凭证](../configuration/alibaba-cloud-credentials.md)。已保存的配置可由 TUI 复用。

2. 打开交互终端，切换到项目目录后启动：

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. 出现停止维护提示和输入区后，输入需求并提交。模型回复和工具执行过程会显示在终端中；出现提问或权限确认时，按终端提示作出选择。

4. 输入 `/help` 查看当前安装版本支持的命令，`/status` 查看当前会话状态，`/exit` 退出终端界面。

TUI 需要交互终端。脚本或 CI 等没有交互终端的场景，请使用[非交互自动化](../automation/non-interactive-mode.md)。不带子命令运行 `iac-code` 默认打开 Web。

## 继续已有终端会话

从会话所属的项目目录运行。使用会话 ID 或唯一名称恢复指定会话：

```bash
iac-code tui --resume <session-id-or-name>
```

也可以继续当前项目最近一次会话：

```bash
iac-code tui --continue
```

`--resume` 和 `--continue` 不能同时使用。当前安装版本的启动参数以 `iac-code tui --help` 为准。

## 转到 Web 或桌面版

1. 运行 `iac-code web`，或打开已安装的桌面应用，选择同一个项目目录。

2. 使用同一个操作系统用户和 `IAC_CODE_CONFIG_DIR` 时，可复用已保存的模型配置、云凭证和指令记忆。后续通过设置管理，具体步骤见[常用操作](./commands.md)。

3. 新建对话继续工作。需要查看旧终端会话时，在**设置 → 常规 → 外来会话可见性**中启用相应的只读显示选项。共用会话存储不代表终端会话可以在 Web/桌面版中继续编辑，详见[会话管理](./sessions.md)。
