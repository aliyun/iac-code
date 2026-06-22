# Pipeline 图片能力合入说明

日期：2026-06-22

## 背景

本次将工作树 `.worktrees/pipeline-image` 中的提交 `6f8cc79 feat: support pipeline image inputs` cherry-pick 回 `fix_pipeline` 分支所在的主工作区 `/Users/ehzyo/open_repo/iac-code3`。

cherry-pick 过程未出现代码冲突，也未出现需要手工合并的国际化 `messages.po` 冲突。合入后测试发现旧的图片提示文案仍残留在翻译目录中，因此执行了 `make translate` 重新抽取、更新并编译翻译文件，确认 active `messages.po` 词条与当前源码一致，旧词条以 Babel 的 obsolete entry 形式保留，没有丢失。

## 合入内容

### Pipeline 图片输入

- 新增 `PipelineUserInput`，让 pipeline 能在文本输入之外携带 `ImageBlock`。
- pipeline runner、sub-pipeline、interrupt、rollback、会话恢复路径支持图片输入。
- judge、step 执行、resume recovery 在需要时保留图片块，避免恢复后只剩纯文本占位。

### REPL 图片体验

- REPL prompt input、renderer、会话恢复显示支持可点击的历史图片引用。
- 修复恢复会话后继续粘贴图片时的图片编号与历史图片冲突问题。
- 补充 REPL pipeline e2e 场景，覆盖图片初始输入、等待输入、选择、回滚、恢复等路径。

### A2A 图片输入

- A2A part 转换支持 raw/data/file URL 形式的图片输入，并统一转为 `ImageBlock`。
- A2A pipeline executor 支持图片输入、等待输入恢复、正常模式 handoff 等场景。
- 当前模型不支持图片时，A2A pipeline 请求返回 JSON-RPC `INVALID_PARAMS`，不再把 task 标记为 failed，避免无效输入污染 pipeline 状态。
- A2A debugger 支持选择图片文件、纯图片 prompt、JSON-RPC error 转 SSE error 展示。

### 测试与文档

- 新增 A2A 图片 e2e 固定图片 fixture，减少手工/e2e 测试消耗。
- 更新 A2A e2e README、REPL e2e README、手工测试指南。
- 新增/更新 pipeline、A2A、REPL、provider、image store/processor 相关单测与 e2e 脚本测试。
- 新增售卖 pipeline 测试模板文件。

## 验证结果

已在主工作区运行：

- `make translate`
- `make format`
- `make lint`
- `make test`

验证中补充适配了目标分支已有 REPL pipeline 测试断言：这些测试原本断言纯字符串输入，合入图片能力后 pipeline 会收到 `PipelineUserInput`，因此同步更新为结构化输入断言。
