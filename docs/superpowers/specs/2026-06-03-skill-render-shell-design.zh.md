# Skill Renderer Shell 边界设计

## 问题

当前 `SkillRenderer` 会把整个 skill 当成一个字符串来处理：

1. 替换 skill 参数，例如 `$ARGUMENTS`、`$0` 和命名参数；
2. 替换内置变量，例如 `${SKILL_DIR}`；
3. 扫描替换后的文本，查找 renderer shell 语法；
4. 执行匹配到的 shell 片段。

这个处理顺序会让用户或模型传入的 skill 参数改变 renderer 最终执行的内容。它也会让 skill 参数语法和 shell 片段中的普通 shell 变量语法发生冲突。

存在风险的场景包括：

- 普通文本占位符被渲染成新的 `````!` shell block；
- 已存在的 shell 命令中的占位符被渲染成额外的 shell 语法；
- block shell 的输出中包含 inline shell 语法，并在后续扫描中被再次执行。

## 目标

- 将 skill 参数视为数据，而不是可执行的 renderer shell 代码。
- 只执行原始 skill 文件中已经存在的 shell 片段。
- 保留显式写在 skill 中的 renderer shell 功能。
- 保持权限检测和 renderer 执行检测使用一致的 shell 语法判断。
- 覆盖模型调用 `SkillTool` 和用户直接调用 skill command 两条路径，因为它们共享 renderer 流程。

## 非目标

- 完全移除 renderer shell 支持。
- 为每条 renderer shell 命令增加细粒度用户确认。
- 改变 bundled skill 的信任规则。
- 重构更大范围的 tool permission pipeline。

## 设计

renderer 会在任何参数替换之前，先把原始 skill 内容解析成有序片段：

- text segment；
- 匹配 ``!`command``` 的 inline shell segment；
- 匹配 `````! ... ````` 的 block shell segment。

只有在原始解析阶段发现的 shell segment 才是可执行的。renderer 不再扫描完整渲染后的 prompt 来发现新的 shell 片段。

text segment 会正常进行 skill 参数替换。如果某个 text segment 被渲染成 ``!`...``` 或 `````! ... `````，这些内容仍然只是普通 prompt 文本，不会被执行。

shell segment 不进行 skill 参数替换。`$ARGUMENTS`、`$0`、`$1`、`$name`、`$PATH`、`$HOME` 等字符串都会保留为原始 shell 命令的一部分。这样可以避免把 shell 变量误当成 skill 占位符，也能防止参数参与构造命令。

`${SKILL_DIR}` 和 `${SESSION_ID}` 这类内置变量不是用户或模型传入的参数。实现时应该保持它们当前的替换行为，同时确保它们不会导致新渲染出的 shell 语法被扫描并执行。

shell 执行输出会作为原始 shell segment 的替换结果拼回最终 prompt。这个输出不会被再次扫描，所以即使输出中包含 shell 语法，也不会触发二次执行。

## 组件

`src/iac_code/skills/renderer.py`

- 增加一个小的 segment model，用来表示 text、inline shell 和 block shell segment。
- 增加 parser，复用现有 shell regex，从原始内容中生成有序、非重叠的 segment。
- 更新 `render_skill_prompt()`：先解析原始内容，再渲染 text segment，执行原始 shell segment，最后拼接结果。
- 更新或替换 `execute_shell_commands()`：基于原始解析得到的 shell segment 执行，而不是反复扫描已经变更过的 prompt。
- 增加共享的 `contains_shell_commands(content: str) -> bool` helper，基于同一个 parser 或同一组 regex 规则。

`src/iac_code/skills/skill_tool.py`

- 更新 `_has_only_safe_properties()`，改为使用 renderer 共享的 shell 检测 helper。权限检查应该用 renderer 实际会执行的同一套语法来判断 skill 是否包含 shell。

测试

- 扩展 `tests/skills/test_renderer.py`，覆盖分段和注入行为。
- 扩展 `tests/skills/test_skill_tool.py`，覆盖共享 shell 检测。

## 数据流

对于一次 skill command：

1. 加载原始 `skill.content`。
2. 如果需要，添加 skill root 前缀，并把它作为 text segment。
3. 将原始 skill 内容解析成 text segment 和 shell segment。
4. 对 text segment 渲染 skill 参数和内置变量。
5. 对 shell segment 执行原始 shell 命令文本，不做 skill 参数替换。
6. 拼接渲染后的文本和 shell 输出。
7. 像现在一样，在 `process_prompt_command()` 中包装最终 prompt。

## 错误处理

参数生成的 shell 语法不再是错误，因为它已经不具备可执行性。它会作为普通 prompt 文本保留。

shell 执行失败继续沿用 `_run_shell()` 的现有行为，根据情况返回 shell error marker 或命令输出。

parser 的边界情况应该优先按文本处理，而不是优先执行。如果语法不完整，或者不匹配 renderer shell pattern，就保留为普通文本。

## 测试计划

- text placeholder 被渲染成 shell block 语法时，不调用 `_run_shell()`。
- text placeholder 被渲染成 inline shell 语法时，不调用 `_run_shell()`。
- shell block 中包含 `$ARGUMENTS`、`$0`、`$1` 或 `$name` 时，执行的仍是原始命令文本，不做 skill 参数替换。
- 原始 inline shell 仍然会执行，并用 trim 后的输出替换 inline segment。
- 原始 block shell 仍然会执行，并用输出替换 block segment。
- block shell 输出中包含 inline shell 语法时，不会被再次扫描或执行。
- 多个 shell 片段和文本片段能够保持原始顺序。
- `_has_only_safe_properties()` 对 renderer 实际会执行的 inline 和 block shell 语法返回 false。

## 验证

先运行聚焦测试：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py tests/skills/test_skill_tool.py -v
```

再运行更宽一些的相关测试：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills tests/services/permissions/test_pipeline.py -v
```

当前全量测试基线有一个无关的 i18n 失败：`src/iac_code/i18n/messages.pot` 缺失。如果运行全量验证，需要把这些基线失败和本次改动导致的问题区分开。
