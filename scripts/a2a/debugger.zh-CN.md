# A2A Pipeline Debugger

`scripts/a2a/debugger.py` 的本地简明使用说明。

## 启动 iac-code A2A

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299 \
  --thinking-exposure raw-thinking --thinking-exposure tool-trace
```

如果本地 smoke 测试不希望停在工具权限确认上：

```bash
cat >/tmp/iac-code-a2a-auto-approve.yml <<'EOF'
auto_approve_permissions: true
thinking_exposure:
  - raw-thinking
  - tool-trace
EOF

PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299 \
  --config /tmp/iac-code-a2a-auto-approve.yml
```

Debugger 的 `Stream` 按钮会在每条消息里发送
`metadata.iac_code.thinking.enabled=true`。Server 仍然需要在 `thinking_exposure`
中开启 `raw-thinking`，才会把 `thinking_delta` 事件推回 debugger。

## 启动 Debugger

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/debugger.py --port 41880 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
```

`--default-cwd` 会作为 `metadata.iac_code.cwd` 随每条消息发送给 A2A server。它表示
task 在 server 侧使用的 workspace，不只是 debugger 自己的工作目录。

A2A server 会在运行 agent 前校验这个路径。默认情况下，server 接受自己的启动目录和
Python 临时目录。如果 `--default-cwd` 位于允许的根目录下但目录尚不存在，server 可能会创建它。
当解析后的路径逃逸允许根目录、无法创建或不能作为目录使用时，请求会被拒绝，并返回
`Invalid A2A workspace metadata.`。

可使用以下任一方式：

```bash
# 使用已运行 server 接受的 cwd 启动 debugger。
uv run python scripts/a2a/debugger.py --port 41880 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "/path/to/server/workspace"
```

```bash
# 或在启动 server 时显式允许 debugger/client workspace。
IACCODE_A2A_ALLOWED_CWDS="/path/to/server/workspace:/path/to/client/workspace" \
IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299
```

打开：

```text
http://127.0.0.1:41880
```

## 基本流程

1. 点击 `Health` 验证 A2A server。
2. 输入 prompt，然后点击 `Stream`。
3. 使用 `Structured Pipeline` 查看可读的树形视图。
4. 使用 `SSE Events` 查看原始事件行和可展开详情。
5. 使用 `Fetch State` 加载当前 pipeline snapshot。
6. 使用 `Cancel` 取消活跃 task。
7. 使用 `Export HTML` 导出只读 snapshot 页面，便于分享。

## 日志与回放

Debugger 启动时会打印每次运行的日志目录，例如：

```text
A2A pipeline debugger logs: /tmp/iac-code-a2a-debugger-runs/<run-id>
```

回放已保存的运行：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/debugger.py --port 41880 \
  --load-log-dir /tmp/iac-code-a2a-debugger-runs/<run-id>
```

## 说明

- Debugger 是本地开发工具，不提供认证能力。
- `contextId` 标识一次会话；`taskId` 标识一个 A2A task。
- `pipeline_handoff_ready` 之后，后续消息通常会在同一个 context 中启动新的 normal-chat task。
- 图片输入只接受支持的图片 MIME 类型：`image/png`、`image/jpeg`、`image/webp` 和 `image/gif`。
- A2A part parser 限制：text inline/raw 和 text `file://` part 限制为 1 MiB；binary inline/raw/data part 限制为 5 MiB；binary `file://` part 限制为 25 MiB。Debugger 上传的每张图片限制为 5 MiB。
- `file://` 图片输入必须解析到一个已存在的本地文件，并且同时位于请求 cwd 和配置的 A2A allowed cwd 根目录下。任一边界之外的本地 URL 都会被拒绝。
- A2A debugger 会发送 image parts。Selling Console Web UI 当前只发送文本输入。
