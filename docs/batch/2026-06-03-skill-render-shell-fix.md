# Skill Renderer Shell 执行隔离修复合入说明

日期：2026-06-03

目标分支：`fix_issue_260603`

来源分支：`skill-render-shell`

来源提交：`a64c91a fix: isolate skill renderer shell execution`

目标提交：`92b1d04 fix: isolate skill renderer shell execution`

## 背景

本次合入用于修复 GitHub issue #68 中 skill renderer 的 shell 执行边界问题。

旧实现会先对整篇 skill 内容替换 `$ARGUMENTS`、`$0`、`$name` 等 skill 参数，再扫描替换后的全文并执行 renderer shell 语法。这会导致用户或模型传入的参数可以生成新的 `!` inline shell 或 ```! block shell 语法，并在没有经过 bash tool 权限路径的情况下被 renderer 直接执行。

## 本次做了什么

### Renderer 分段渲染

- 新增原始 prompt 分段逻辑，将 skill 内容先解析为 text segment、inline shell segment 和 block shell segment。
- 只执行原始 skill 文件中已经存在的 shell segment。
- text segment 继续渲染 skill 参数；如果参数渲染出了 `!` inline shell 或 ```! block shell 语法，这些内容只作为普通 prompt 文本保留，不会执行。
- shell segment 不渲染 skill 参数，因此 `$ARGUMENTS`、`$0`、`$1`、`$name`、`$PATH`、`$HOME` 等内容保留为 shell 自身语义，避免和 skill 参数语法冲突。
- shell 输出不会再次被扫描，因此 shell 输出中包含的 renderer shell 语法不会触发二次执行。

### 内置变量处理

- `${SKILL_DIR}` 和 `${SESSION_ID}` 仍按现有行为替换。
- 内置变量替换不会导致新生成的 shell 语法被再次扫描执行。

### 权限检测对齐

- `SkillTool._has_only_safe_properties()` 不再使用简单字符串包含判断。
- 权限检测改为复用 renderer 的 shell 检测 helper，确保“权限判断认为有 shell”和“renderer 实际会执行 shell”的语法规则一致。

### 测试覆盖

新增和增强了 skill renderer / skill tool 测试，覆盖：

- 参数渲染出的 block shell 不执行。
- 参数渲染出的 inline shell 不执行。
- shell segment 内的 `$ARGUMENTS`、`$0`、`$name` 不被 skill 参数替换。
- shell segment 内的 `${SKILL_DIR}` 和 `${SESSION_ID}` 仍会替换。
- 原始 inline shell 和 block shell 仍会执行。
- shell 输出不会被二次扫描。
- 多个原始 shell segment 保持顺序执行。
- skill 权限检测复用 renderer shell 判断。

## Cherry-pick 结果

在 `/Users/ehzyo/open_repo/iac-code` 中执行 cherry-pick：

```bash
git cherry-pick a64c91a
```

结果：

- cherry-pick 干净完成。
- 没有文件冲突。
- 没有 `message.po` 冲突。
- 未修改翻译文件。

## 翻译处理

本次生产代码没有新增 `_()` 包裹的用户可翻译字符串，主要新增内容是 renderer 内部 helper、测试和设计/计划文档。

cherry-pick 没有出现 `message.po` 冲突，因此没有运行 `make translate`。如果后续合入时出现 `src/iac_code/i18n/locales/*/LC_MESSAGES/messages.po` 冲突，应保留双方新增词条，再运行：

```bash
PATH="$HOME/.local/bin:$PATH" make translate
```

运行后需要检查：

- 新增词条没有丢失。
- 现有翻译没有被覆盖成空值或英文占位。
- 仅由 Babel 更新的行号、`POT-Creation-Date` 等元数据变化符合预期。

## 兼容性判断

### 公共 API

CLI 入口、`SkillDefinition.get_prompt()`、`process_prompt_command()` 和 `SkillTool` 的公开 tool schema 均未改变。现有调用方不需要调整参数或返回值处理。

### Skill 兼容性

显式写在 skill 文件中的 renderer shell 仍会执行，保留原有能力。

行为变化集中在参数边界：

- 以前参数可以渲染出新的 renderer shell 并被执行；现在只会作为普通文本。
- 以前 shell segment 内的 `$ARGUMENTS`、`$0`、`$name` 可能被 skill 参数替换；现在保留为 shell 自身变量语义。
- 以前 block shell 输出后仍可能被 inline shell 扫描；现在输出不会二次执行。

这些变化是本次安全修复的预期行为。依赖“参数构造 renderer shell 命令”的 skill 会受到影响，但该模式本身就是本次修复要禁止的风险行为。

### 权限兼容性

项目 skill 中原始写入的 renderer shell 仍会被识别为非 safe-only skill，并在模型调用 `SkillTool` 路径下触发原有 skill 级权限询问。

本次未引入 bash tool 级逐命令权限，也没有改变 bundled skill 的信任规则。

### 依赖和数据结构

没有新增依赖，没有数据库或配置 schema 迁移，没有更改用户凭证或 settings 文件格式。

### 风险

主要风险是少量历史项目 skill 可能曾依赖在 shell segment 中用 `$ARGUMENTS` 拼接命令。该行为现在不再支持；推荐将参数作为普通 prompt 文本传给模型，或由模型显式调用受权限管控的 bash tool。

## 验证建议

推荐在合入后运行：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py tests/skills/test_skill_tool.py -v
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills tests/services/permissions/test_pipeline.py -v
PATH="$HOME/.local/bin:$PATH" make lint
PATH="$HOME/.local/bin:$PATH" make test
```

目标 checkout 中实际验证结果：

- `tests/skills/test_renderer.py tests/skills/test_skill_tool.py -v`：`48 passed`。
- `tests/skills tests/services/permissions/test_pipeline.py -v`：`184 passed`。
- `make lint`：通过。
- `make test`：`4026 passed, 244 warnings`。

手动交互验证建议覆盖：

- `$ARGUMENTS` 渲染出的 ```! block 不会执行。
- `$ARGUMENTS` 渲染出的 `!` inline shell 不会执行。
- 原始 skill 文件中显式写入的 renderer shell 仍会执行。
- shell segment 内的 `$ARGUMENTS` 保留为 literal/shell 语义，不被 skill 参数替换。
- shell 输出中包含的 `!` inline shell 不会被二次扫描执行。
