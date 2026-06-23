# 20260623 selling-console cherry-pick

## 背景

将 `/Users/ehzyo/open_repo/iac-code3/.worktrees/codex/selling-pipeline-console` 的最近 1 个提交合回主工作区 `/Users/ehzyo/open_repo/iac-code3` 的 `fix_pipeline` 分支。

## 合入提交

- `722492a feat: add selling pipeline console`

## 主要内容

- 新增 `scripts/a2a/selling_console.py`，提供本地 Selling Pipeline Console 的静态资源服务和 A2A 代理接口。
- 新增 `scripts/a2a/selling_console_web/`，实现售卖页 Web 控制台，包括聊天区、方案卡片、流程进度条、调试面板和交互事件渲染。
- Selling Console Web UI 当前只发送文本输入；A2A 图片输入覆盖应使用 `scripts/a2a/debugger.py` 或直接 A2A 请求。
- 新增 `selling_console_web/README.md`，说明 A2A server 与 Web console 的启动方式。
- 新增进度条视觉方案设计稿 `scripts/a2a/selling_console_web/design/selling-pipeline-progress-options.html`。
- 更新 `scripts/README.md`，补充售卖控制台脚本入口说明。
- 新增前端脚本和 A2A 事件相关测试，覆盖静态资源、代理脚本和 pipeline event 渲染契约。

## 冲突处理

- 本次 cherry-pick 仅自动合并了 `scripts/README.md`，没有出现需要手工处理的代码冲突。
- 本次合入未修改国际化 `messages.po` 文件，没有出现国际化冲突，也没有需要通过 `make translate` 恢复的词条。

## 验证

按要求执行并通过：

- `make lint`：通过，`ruff check` 与 `ty check` 均无错误。
- `make format`：通过，`ruff format` 显示 `744 files left unchanged`。
- `make test`：通过，`6874 passed, 254 warnings in 108.36s`。
