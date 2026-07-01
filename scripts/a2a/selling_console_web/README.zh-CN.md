# Selling Console Web

`scripts/a2a/selling_console.py` 使用的独立静态前端。它用于驱动 selling pipeline、查看步骤进度、
选择候选方案，并在部署后继续进入 normal chat。

当前 Web console 只发送文本输入。A2A image part 的覆盖请使用 `scripts/a2a/debugger.py`。

## 运行

从仓库根目录开始，先启动 A2A server：

```bash
PATH="$HOME/.local/bin:$PATH" IAC_CODE_MODE=pipeline \
uv run iac-code a2a --transport http --host 127.0.0.1 --port 41299
```

然后启动 Web console：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/selling_console.py --port 41980 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
```

然后打开 `http://127.0.0.1:41980`。

## 文件

- `index.html` 渲染页面外壳。
- `styles.css` 包含布局、聊天、方案卡片和进度视觉样式。
- `app.js` 处理 A2A stream 解析、UI 状态、调试控件和交互。
- `design/` 保存进度变体的独立视觉探索稿。

调试面板默认折叠。只有在检查连接设置、进度变体参数、context ID 或最近 stream 诊断信息时，
才需要展开它。
