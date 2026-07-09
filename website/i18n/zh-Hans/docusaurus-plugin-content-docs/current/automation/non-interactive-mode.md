---
title: 非交互模式
description: 从参数或 stdin 运行一次性提示词。
---

# 非交互模式

非交互模式会执行单条提示词并退出。它适合让 IaC Code 为可重复任务生成输出，而不进入 REPL。

使用 `--prompt` 直接传入提示词：

```bash
iac-code --prompt "创建一个 OSS Bucket"
```

使用 `--prompt -` 从标准输入读取提示词：

```bash
echo "创建一个 VPC 和两台 ECS 实例" | iac-code --prompt -
```

当调用方需要结构化输出时，使用 `--output-format`：

```bash
iac-code --prompt "创建一个 OSS Bucket" --output-format json
```

使用 `--max-turns` 限制代理最多可以工作多少轮：

```bash
iac-code --prompt "创建一个 VPC" --max-turns 20
```

支持的输出格式包括：

| 格式 | 用途 |
|---|---|
| `text` | 面向用户阅读的文本输出，默认使用该格式。 |
| `json` | 返回单个 JSON 结果，适合调用方解析最终响应。 |
| `stream-json` | 输出流式 JSON 事件，适合调用方处理增量进度。 |

## SDK Process 模式

SDK process 模式用于让客户端把 `iac-code` 作为长期运行的子进程，并通过 stdin/stdout 交换按行分隔的 JSON：

```bash
iac-code --input-format stream-json --output-format stream-json
```

这不同于一次性的 `--prompt` 模式。`--input-format stream-json` 必须与 `--output-format stream-json` 一起使用，并且会拒绝 `--prompt`。调用方必须先发送 `initialize` control request，然后才能发送用户消息：

```json
{"type":"control_request","request_id":"req-init","request":{"subtype":"initialize","cwd":"/absolute/workspace","model":"qwen3.7-max"}}
{"type":"user","request_id":"req-1","session_id":"session-1","message":{"role":"user","content":"创建一个 OSS Bucket"},"metadata":{"iac_code":{"cwd":"/absolute/workspace"}}}
{"type":"control_request","request_id":"req-end","request":{"subtype":"end_session"}}
```

进程会输出 `control_response`、`stream_event`、`error` 和最终 `result` frame。流式事件复用一次性输出 streaming 使用的公开 `stream-json` event 结构。initialize payload 或 `metadata.iac_code.cwd` 中的 `cwd` 必须是绝对路径。

支持的控制流程包括 `initialize`、`interrupt`、`set_model`、`end_session`、`close`、`keep_alive` 和 `update_environment_variables`。同一个 process 内一次只能运行一个用户 turn。如果两个子进程尝试在同一个 `cwd` 下并发使用同一个 `session_id`，第二个 turn 会收到可重试的 `session_busy` 错误。

当 `IAC_CODE_MODE=pipeline` 时，process 模式会执行 Pipeline 模式，而不是普通 agent loop。Pipeline stream frame 使用 `type: "pipeline_event"`，最终 result frame 会包含 `pipeline` 对象，其中有 `contextId`、`taskId`、`iacCodeSessionId`、`status` 和 `sidecarStatus`。恢复可继续的 pipeline 时，需要发送同一个 `contextId` 和活跃的 `taskId`；如果 context 中存在可恢复 task 但调用方省略了 task id，process 会返回可重试的 `pipeline_task_required` 错误，并携带 `recoverableTaskId`。

## 会话备份

设置 `IAC_CODE_CONFIG_BACKUP_DIR` 后，非交互运行会在关键检查点镜像 v2 session。普通轮次结束检查点使用 `normal_turn_end`；此时备份失败只会记录为 `warning` 并写入 `.backup-state.json`，不会让已完成响应失败，也不会在最终输出中新增 warning 字段。Pipeline 模式有自己的关键备份 gate。

## 自动化中的权限控制

在非交互模式下运行时，使用 `--permission-mode` 控制代理处理工具审批的方式：

```bash
iac-code --prompt "部署资源栈" --permission-mode bypass_permissions
```

在 `bypass_permissions` 下，工具操作会被自动批准（安全检查除外），但任何需要审计记录的允许决策在审计持久化失败时都会 fail closed。阿里云写 API 在 `bypass_permissions` 之外仍有单独保护；对于更窄范围的可信自动化，请不要使用 `bypass_permissions`，而是显式允许每个需要的写 API：

```bash
iac-code --prompt "部署资源栈" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

要限制代理的操作范围，可以组合使用 `--allowed-tools` 和 `--disallowed-tools`：

```bash
iac-code --prompt "检查资源栈状态" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

完整启动参数请参见[命令行选项](../cli/command-line-options.md)。
