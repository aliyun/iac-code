# Pipeline 图片能力手工测试指南

本文用于手工验证 pipeline 在 REPL 和 A2A 模式下的图片输入、会话恢复、normal handoff
和 interrupt/rollback 能力。

## 测试范围

覆盖以下主链路：

- REPL 首轮图片输入启动 pipeline。
- REPL 等待输入时用图片回答方案选择或 `ask_user_question`。
- REPL pipeline 完成并 handoff 到 normal chat 后继续用图片追问。
- REPL 通过 `--continue` / `--resume` / `/resume` 恢复 session。
- A2A 通过 debugger 上传图片并驱动 pipeline。
- A2A 重启后仅带 `contextId`、不带 `taskId` 时 hydrate 回原 pipeline task。
- A2A 图片 interrupt/rollback 后恢复，并最终切换到安全组任务。

## 前置条件

从当前仓库 checkout/worktree 的根目录运行即可：

```bash
cd /path/to/iac-code
```

建议使用支持图片输入的模型：

```bash
export IAC_CODE_MODE=pipeline
export IAC_CODE_PROVIDER=dashscope
export IAC_CODE_MODEL=qwen3.6-plus
export IAC_CODE_CWD="$PWD"
```

固定测试图片素材在：

```text
scripts/a2a/e2e/fixtures/text-images/
```

当前固定图片包括：

- `initial.png`：选择一个已有vpc，创建一个vswitch
- `selection.png`：你随便选一个方案。
- `normal-followup.png`：你刚才创建了什么
- `ask-first-answer.png`：创建云网络资源，只选择已有 VPC 创建 VSwitch
- `ask-second-answer.png`：选择已有 VPC 创建 VSwitch，地域/可用区/网段使用推荐值
- `rollback-interrupt.png`：回退到 intent_parsing，创建安全组

这些测试会调用真实 provider 和阿里云路径。执行部署相关场景前，请确认账号、凭证、
资源权限和费用影响符合预期。

## REPL 手测

启动 REPL：

```bash
uv run iac-code
```

如果本机没有 `uv`，可使用：

```bash
.venv/bin/python -m iac_code.cli.main
```

REPL 中图片输入方式：

- 复制图片到系统剪贴板后，在 REPL 中按 `Ctrl+V` / `Cmd+V`。
- 或直接复制/粘贴图片文件路径，例如
  `scripts/a2a/e2e/fixtures/text-images/initial.png`。
- 成功后输入框里会出现类似 `[Image #1]` 的占位符。

建议每次图片输入前加一段读图提示：

```text
请读取图片中的文字，并将图片中的文字作为本轮用户输入执行。
```

### REPL-1：首轮图片启动 pipeline

操作：

1. 在 REPL 输入读图提示。
2. 粘贴 `scripts/a2a/e2e/fixtures/text-images/initial.png`。
3. 提交。
4. 到方案选择阶段后，输入 `你随便选一个方案。`。

验收：

- pipeline 能识别图片里的“选择一个已有vpc，创建一个vswitch”。
- 能进入方案选择阶段。
- 选择后 pipeline 能继续部署并完成。
- 最终输出包含 VSwitch / 交换机相关证据。

### REPL-2：方案选择阶段用图片回答

操作：

1. 文本启动 pipeline：

   ```text
   选择一个已有vpc，创建一个vswitch
   ```

2. 到候选方案选择阶段后，输入读图提示。
3. 粘贴 `scripts/a2a/e2e/fixtures/text-images/selection.png`。
4. 提交。

验收：

- 图片里的“你随便选一个方案。”被接受为选择输入。
- pipeline 继续执行。
- 最终完成并产生 VSwitch 证据。

### REPL-3：ask_user_question 用图片回答

操作：

1. 输入模糊需求：

   ```text
   我有个产品要上线
   ```

2. 如果 pipeline 进入追问，输入读图提示并粘贴：

   ```text
   scripts/a2a/e2e/fixtures/text-images/ask-first-answer.png
   ```

3. 如继续追问，再输入读图提示并粘贴：

   ```text
   scripts/a2a/e2e/fixtures/text-images/ask-second-answer.png
   ```

验收：

- `ask_user_question` 能消费图片回答。
- 后续能进入 VSwitch 方案规划。
- pipeline 能最终完成。

### REPL-4：handoff normal chat 后图片追问

操作：

1. 完成一次 VSwitch pipeline。
2. pipeline handoff 到 normal chat 后，输入：

   ```text
   请读取图片中的文字，并回答图片里的问题。
   ```

3. 粘贴 `scripts/a2a/e2e/fixtures/text-images/normal-followup.png`。

验收：

- normal chat 保持同一个会话上下文。
- 能回答“刚才创建了什么”。
- 不重新进入新的 pipeline。

### REPL-5：session 恢复

操作：

1. 在 pipeline 等待输入、运行中或 handoff normal chat 后退出 REPL。
2. 重新启动：

   ```bash
   IAC_CODE_MODE=pipeline \
   IAC_CODE_PROVIDER=dashscope \
   IAC_CODE_MODEL=qwen3.6-plus \
   uv run iac-code --continue
   ```

3. 或启动后使用：

   ```text
   /resume
   ```

验收：

- 同项目 session 能恢复。
- 如果目标 session 有可恢复 pipeline sidecar，能回到 pipeline 状态。
- 如果 pipeline 已 handoff 到 normal chat，normal chat 历史仍可继续追问。

## A2A 手测

### 启动 A2A server

建议使用单独的持久化目录，便于重启恢复和清理：

```bash
cat >/tmp/iac-code-a2a-image-manual.yml <<EOF
host: 127.0.0.1
port: 41399
transport: http
persistence_dir: /tmp/iac-code-a2a-image-manual/persistence
artifact_dir: /tmp/iac-code-a2a-image-manual/artifacts
auto_approve_permissions: true
EOF
```

启动 server：

```bash
PATH="$HOME/.local/bin:$PATH" \
IAC_CODE_MODE=pipeline \
IAC_CODE_PROVIDER=dashscope \
IAC_CODE_MODEL=qwen3.6-plus \
IAC_CODE_CWD="$PWD" \
uv run iac-code a2a --config /tmp/iac-code-a2a-image-manual.yml
```

### 启动 A2A debugger

另开终端：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/debugger.py --port 41880 \
  --default-server-url http://127.0.0.1:41399 \
  --default-cwd "$PWD"
```

打开：

```text
http://127.0.0.1:41880
```

debugger 支持上传 `image/png`、`image/jpeg`、`image/webp`、`image/gif`，单图最大 5 MiB。直接构造 A2A 请求时，inline payload 和本地文件 payload 同样受 A2A part parser 的大小限制；`file://` 必须解析到请求 cwd 或其它允许读取的 root 内，越权本地 URL 会被拒绝。Selling Console Web UI 当前只发送文本输入，图片手测请使用 debugger。

### A2A-1：首轮图片启动 pipeline

操作：

1. 点击 `Health`，确认 A2A server 正常。
2. 文本框输入：

   ```text
   请读取图片中的文字，并将图片中的文字作为本轮用户输入执行。
   ```

3. 上传 `scripts/a2a/e2e/fixtures/text-images/initial.png`。
4. 点击 `Stream`。
5. 记录页面中的 `contextId` 和 `taskId`。
6. 到选择阶段后，在同一个 `contextId` 下发送 `你随便选一个方案。`。

验收：

- `Structured Pipeline` 能看到 pipeline 树推进。
- `SSE Events` 能看到 pipeline event。
- 能进入候选选择并最终完成。
- 最终有 VSwitch 证据。

### A2A-2：重启后图片回答 selection

操作：

1. 文本启动：

   ```text
   选择一个已有vpc，创建一个vswitch
   ```

2. 等待进入候选选择阶段。
3. 停止 A2A server。
4. 使用同一份 config 重启 A2A server。
5. debugger 保留同一个 `contextId`，`taskId` 留空。
6. 输入读图提示，上传 `selection.png`，点击 `Stream`。

验收：

- 不带 `taskId` 也能 hydrate 回原 pipeline task。
- pending selection 被消费。
- pipeline 完成并产生 VSwitch 证据。

### A2A-3：重启后图片回答 ask_user_question

操作：

1. 发送模糊输入：

   ```text
   我有个产品要上线
   ```

2. 等待进入 `ask_user_question`。
3. 停止并重启 A2A server。
4. debugger 使用同一个 `contextId`，`taskId` 留空。
5. 输入读图提示，上传 `ask-first-answer.png`。
6. 如再次追问，继续上传 `ask-second-answer.png`。

验收：

- 重启后 state 仍能显示 pending ask。
- 不带 `taskId` 的图片回答能绑定回原 task。
- pipeline 最终完成并产生 VSwitch 证据。

### A2A-4：handoff normal chat 后图片追问

操作：

1. 让 pipeline 完成并 handoff 到 normal chat。
2. 在同一个 `contextId` 下，`taskId` 留空。
3. 输入：

   ```text
   请读取图片中的文字，并回答图片里的问题。
   ```

4. 上传 `normal-followup.png`。
5. 点击 `Stream`。
6. 停止并重启 A2A server。
7. 再用同一个 `contextId` 发送 normal chat 恢复问题，例如：

   ```text
   我刚才问了你哪些问题？
   ```

验收：

- 图片 normal follow-up 使用新的 normal-chat task。
- 仍保持同一个 `contextId`。
- 重启后 completed pipeline handoff state 仍可恢复。
- normal chat 能继续回答历史相关问题。

### A2A-5：图片 interrupt / rollback

操作：

1. 启动 VSwitch pipeline，等进入候选执行或 running 状态。
2. 使用同一个 `contextId`。
3. 输入读图提示。
4. 上传 `rollback-interrupt.png`。
5. 点击 `Stream`。
6. 可在 rollback 后停止并重启 A2A server。
7. 继续发送：

   ```text
   继续
   ```

验收：

- 能看到 rollback / interrupt 相关 pipeline event。
- pipeline 回到 `intent_parsing`。
- 最终部署目标是安全组。
- 最终部署证据不是 VSwitch。

## 辅助检查

查询 pipeline state：

```bash
curl 'http://127.0.0.1:41399/iac-code/pipeline/state?contextId=<contextId>' | jq
```

查询 task：

```bash
uv run iac-code a2a-client task-get \
  --url http://127.0.0.1:41399 \
  --task-id <taskId>
```

列出同一 context 下的 task：

```bash
uv run iac-code a2a-client task-list \
  --url http://127.0.0.1:41399 \
  --context-id <contextId> \
  --output json
```

debugger 启动时会打印日志目录，例如：

```text
A2A pipeline debugger logs: /tmp/iac-code-a2a-debugger-runs/<run-id>
```

重点查看：

- debugger 页面里的 `Structured Pipeline`
- debugger 页面里的 `SSE Events`
- debugger 页面里的 `Fetch State`
- `/tmp/iac-code-a2a-debugger-runs/<run-id>` 下的请求和响应日志
- `/tmp/iac-code-a2a-image-manual/persistence` 下的 A2A context/task 持久化数据

## 总体验收标准

- 图片输入能被模型识别为有效用户意图。
- REPL 和 A2A 都能在 pipeline 初始输入、等待输入、handoff normal chat、interrupt 中消费图片。
- session 重启恢复后，状态不丢失。
- A2A 仅带 `contextId`、不带 `taskId` 时，能恢复到原 pipeline task。
- VSwitch 场景最终产生 VSwitch / 交换机证据。
- interrupt 场景最终产生安全组证据，且不是 VSwitch。
