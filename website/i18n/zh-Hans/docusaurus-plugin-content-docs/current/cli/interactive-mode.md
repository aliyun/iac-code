---
title: 交互模式
description: 使用 REPL 进行迭代式基础设施工作。
---

# 交互模式

不带参数运行即可进入交互式 REPL：

```bash
iac-code
```

当你需要在多轮对话中细化基础设施需求时，交互模式很适合。

从认证开始：

```text
/auth
```

然后描述你想构建的内容：

```text
创建一个 VPC、两台 ECS 实例，以及一个允许办公 IP 通过 SSH 访问的安全组。
```

## 命令

输入 `/` 可以发现可用的 Slash 命令。常用运维命令包括：用 `/status` 查看当前会话状态，用 `/skills` 管理技能，用 `/memory` 查看已保存记忆，用 `/rename` 命名当前会话，以及用 `/resume` 切换会话。

输入 `$` 只会发现并调用技能。

## 编辑输入

使用 `Shift+Enter` 可以插入换行而不发送 prompt。单独按 `Enter` 会提交完整 prompt。

如果你的终端无法单独上报 `Shift+Enter`，可以先按 `Esc` 再按 `Enter` 来插入换行。多行 prompt 会作为一个完整的历史记录保存，因此按 `Up` 可以恢复完整 prompt。

## Shell escapes

在一行开头输入 `!`，可以在 REPL 中通过内置 `bash` 工具运行本地 shell 命令：

```text
!pwd
!git status --short
```

IaC Code 会应用正常的工具权限检查，在当前项目上下文中运行命令，并把输出显示在终端里。该命令不会作为聊天消息发送给模型。
