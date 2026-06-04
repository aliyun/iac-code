# UI Status, History, and i18n Fixes

## 背景

本次变更对应 GitHub issue #92、#84、#85 和 #76。实现先在隔离 worktree
`codex/ui-status-history-i18n` 中完成，并将该 worktree 中的多枚实现、测试和翻译提交压缩为一个提交：

- `2667542 fix: resolve status history model i18n issues`

随后将该提交 cherry-pick 到主 checkout `/Users/ehzyo/open_repo/iac-code` 的
`fix_issue_260603` 分支，并将本文档 amend 进同一个批处理提交。

## 变更内容

- 修复 #92：`ShellHistoryProvider` 现在按历史文件路径、`mtime_ns` 和文件大小缓存解析后的 shell history。历史文件未变化时，连续输入 `!` 不再每次整文件读取和解析，并将返回建议数量限制为 100。
- 修复 #84：`/model` 现在从 `settings["providers"][activeProvider]["apiBase"]` 读取自定义 base URL，用于 telemetry；如果保存值等于 provider 默认值，则不当作自定义 URL 上报。模型切换时也会保留已有自定义 `apiBase`，避免覆盖为默认值。
- 修复 #84：`/status` 的紧凑数字格式不再用整数除法截断，`1500` 会显示为 `1.5k`。
- 修复 #85：resume picker 的 minutes、hours、days、messages 和 more lines 文案改用 `ngettext(...).format(n=...)`，不再硬编码英文 `s` 后缀。
- 修复 #85：更新 `messages.pot`、zh/ja/es/fr/de/pt 的 `messages.po` 和运行时 `messages.mo`，并在 `.gitignore` 中保留通用 `*.pot` 忽略的同时允许跟踪 `src/iac_code/i18n/messages.pot`。
- 按中等范围修复 #76：`TokenCounter` 新增 tool definition token 估算，包含固定 overhead、工具名、描述和 input schema 文本。
- 按中等范围修复 #76：非 OpenAI 模型不再静默使用 `cl100k_base` 作为 fallback。Qwen/QWQ/Kimi/GLM/Doubao/MiniMax/Gemini 及 unknown/default 使用模型族字符/token 比例，并对 CJK 字符做更保守估算。
- 按中等范围修复 #76：`ContextManager` 的 `get_total_tokens()`、`get_usage()`、`usage_percent` 和 `needs_compaction()` 都包含 tool definition tokens；`AgentLoop` 在初始化、provider 切换和每轮 provider 调用前同步当前工具定义。

## 兼容性判断

- `/status` 首次进入会比旧版本显示更高的上下文占用，这是预期行为。它现在表示下一次模型请求预计携带的 system prompt、tool definitions 和消息总量，而不是单纯的对话消息量。
- `/model` 对已有配置兼容：`activeProvider` 仍保持字符串格式，provider 配置仍保存在 `providers.<key>` 下。新增逻辑只读取并保留现有 `apiBase`，不会改变 settings 文件结构。
- shell history 的 suggestion 结果仍保持原有 zsh extended history 解析、最近优先和去重行为，只增加缓存和结果上限。
- i18n runtime 使用 `.mo` 文件，已随 `.po` 一起编译提交；英文默认路径仍使用 source string fallback。
- #76 没有引入 Qwen/Kimi/GLM 等真实 tokenizer SDK，这是已确认的中等修复范围。当前实现降低了明显低估风险，但不是各模型官方 tokenizer 的精确计数。
- `ContextManager` 与主分支已有 compaction 兼容性修复合并后同时保留：tool use/tool result round-trip 保留逻辑继续存在，新增 tool-definition token 统计只影响 token 总量和 compaction 阈值判断。

## Cherry-pick 冲突处理

Cherry-pick 到 `fix_issue_260603` 时只发生一个冲突：

- `tests/services/test_context_manager.py`

冲突原因是主分支已有 `ToolUseBlock` compaction 回归测试，而本次变更新增 `SimpleNamespace` 用于 tool-definition token 测试。解决方式是合并两边 import：

- 保留 `ToolUseBlock`，确保主分支已有 tool round-trip compaction 测试继续运行。
- 保留 `SimpleNamespace`，确保本次新增 tool-definition token 测试继续运行。

本次 cherry-pick 没有发生 `messages.po` 或 `messages.pot` 冲突，因此没有额外运行 `make translate`。翻译文件来自已执行过 `make translate` 并通过 `tests/test_i18n.py` 的 squashed 提交。合入后全量测试发现 `.po` 文件系统时间戳比 `.mo` 新，因此在主 checkout 中执行了 `uv run pybabel compile -d src/iac_code/i18n/locales`，重新编译运行时 catalog 并让 `test_mo_files_up_to_date` 通过。

## 验证

在 `codex/ui-status-history-i18n` worktree 压缩前，最新 HEAD 已执行：

```bash
PATH="$HOME/.local/bin:$PATH" make test
PATH="$HOME/.local/bin:$PATH" make lint
```

结果：

- `make test`：`4056 passed, 244 warnings`
- `make lint`：ruff 和 ty 均通过

在 cherry-pick 到 `/Users/ehzyo/open_repo/iac-code` 后，提交钩子执行通过：

- `make lint` 钩子：通过
- `make format` 钩子：通过
- `tests/test_i18n.py`：`19 passed`

后续如继续修改翻译相关字符串，应重新执行：

```bash
PATH="$HOME/.local/bin:$PATH" make translate
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/test_i18n.py -v
```
