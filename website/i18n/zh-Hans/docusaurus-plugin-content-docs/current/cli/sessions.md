---
title: 会话管理
description: 持久化并恢复跨运行的对话。
---

# 会话管理

IaC Code 会自动将每次对话持久化到磁盘。你可以恢复任何历史会话，从中断处继续工作。

## 恢复会话

### 交互式：`/resume`

在 REPL 中使用 `/resume` 命令：

```text
/resume
```

这会打开交互式选择器，显示当前项目的最近会话。设置了会话名称时会优先用名称作为标题，否则会使用最后一条提示词，或回退到第一条提示词。

通过精确会话 ID、唯一 ID 前缀或唯一会话名称恢复特定会话：

```text
/resume abc123
```

### 命名会话

使用 `/rename` 为当前会话设置一个稳定、易读的名称：

```text
/rename deploy-prod
```

名称会保存在会话元数据中。恢复会话时，它会显示在欢迎横幅、退出提示和 `/resume` 选择器中。

当名称能唯一标识一个会话时，可以按名称恢复：

```text
/resume deploy-prod
iac-code --resume deploy-prod
```

### 命令行：`--resume` 和 `--continue`

从命令行按精确会话 ID、唯一 ID 前缀或唯一会话名称恢复特定会话：

```bash
iac-code --resume <session-id-or-name>
```

恢复最近的会话：

```bash
iac-code --continue
```

也可使用短标志 `-r` 和 `-c`：

```bash
iac-code -r <session-id-or-name>
iac-code -c
```

### 跨项目会话

当会话属于不同的项目目录时，IaC Code 不会直接切换工作目录，而是打印在正确上下文中恢复的命令：

```text
cd /path/to/other/project && iac-code --resume <session-id>
```

该命令会尽可能复制到剪贴板。

## 中断恢复

如果会话在执行过程中被中断（例如工具运行时进程被终止），IaC Code 在恢复时会检测到未完成的工具调用，并附加合成的错误结果。这使模型能够优雅恢复，而不会卡在等待永远不会到达的工具输出上。

## 会话选择器

`/resume` 选择器显示以下信息：

| 列 | 说明 |
|----|------|
| 标题 | 设置了会话名称时显示名称，否则显示最后一条或第一条用户提示词 |
| 分支 | 会话时的 Git 分支 |
| 时间 | 最后修改时间 |

会话按最近优先排序。可以输入文字按标题内容过滤。
