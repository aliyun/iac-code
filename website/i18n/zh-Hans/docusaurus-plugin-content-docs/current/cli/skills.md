---
title: 技能
description: 创建和使用可复用的提示词模板作为 Slash 命令。
---

# 技能

技能是可复用的提示词模板，能够以自定义 Slash 命令的形式扩展 IaC Code 的能力。你可以将复杂的指令、工具配置和工作流模式打包为命名命令，在对话中随时调用。

## 技能文件格式

技能以 Markdown 文件定义，包含 YAML frontmatter。支持两种格式：

### 单文件

以技能名称命名的独立 Markdown 文件：

```text
skills/
  deploy-check.md
  code-review.md
```

### 目录

包含 `SKILL.md` 的目录，适用于技能需要附带额外参考文件的场景：

```text
skills/
  my-skill/
    SKILL.md
    references/
      template.yml
```

## 发现与优先级

IaC Code 从多个位置发现技能。同名技能按后者覆盖前者的规则处理：

| 优先级 | 位置 | 说明 |
|--------|------|------|
| 1（最低） | 内置 | IaC Code 自带的内置技能 |
| 2 | `~/.iac-code/skills/` | 用户全局技能（跟随 `IAC_CODE_CONFIG_DIR`） |
| 3 | `skills/` | 项目级技能目录 |
| 4（最高） | `.iac-code/skills/` | 项目配置级技能目录 |

项目技能目录会从当前工作目录向上搜索至文件系统根目录。

## Frontmatter 参考

每个技能文件以 `---` 分隔符之间的 YAML frontmatter 开头：

```yaml
---
name: deploy-check
description: Verify deployment readiness of the current stack
when_to_use: When the user asks to check or verify a deployment
argument_hint: <stack-name>
arguments:
  - stackName
  - region
allowed_tools:
  - bash
  - aliyun_api
user_invocable: true
model: inherit
effort: ""
context: inline
agent: general-purpose
paths:
  - "*.yml"
  - "templates/**/*.json"
---
```

| 字段 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | **是** | 文件名 | 调用时使用的技能名称，省略时回退为文件名 |
| `description` | **是** | `""` | 命令列表中显示的单行描述 |
| `descriptions` | 否 | `{}` | 按语言代码键入的本地化描述（如 `zh-Hans`） |
| `when_to_use` | 否 | `""` | 提示模型何时自动调用此技能 |
| `argument_hint` | 否 | `""` | 命令名后显示的占位符提示 |
| `arguments` | 否 | `[]` | 用于位置替换的命名参数列表 |
| `allowed_tools` | 否 | `[]` | 技能允许使用的工具（适用于 fork 模式） |
| `user_invocable` | 否 | `true` | 用户是否可以通过 `/name`（或 `$name`）直接调用 |
| `model` | 否 | `"inherit"` | 技能执行时的模型覆盖 |
| `effort` | 否 | `""` | 思考 effort 覆盖 |
| `context` | 否 | `"inline"` | 执行模式：`inline` 或 `fork` |
| `agent` | 否 | `"general-purpose"` | fork 模式使用的 agent 类型 |
| `paths` | 否 | `[]` | 用于路径自动激活的 glob 模式 |

## 执行模式

### Inline（默认）

技能渲染后的内容直接注入当前对话上下文。模型将其视为附加指令，在同一会话中执行。

```yaml
context: inline
```

### Fork

技能在隔离的子 agent 中运行，拥有独立的上下文。子 agent 的最终响应作为工具结果返回。适用于不应污染主对话的独立任务。

```yaml
context: fork
agent: general-purpose
```

## 参数替换

技能内容可以引用用户传入的参数：

| 占位符 | 说明 |
|--------|------|
| `$ARGUMENTS` | 完整的参数字符串 |
| `$0`、`$1`、... | 位置参数（空格分隔，支持引号） |
| `$ARGUMENTS[0]`、`$ARGUMENTS[1]` | 显式索引访问 |
| `$argName` | 命名参数（按 `arguments` 列表中的位置匹配） |

如果内容中未找到占位符，参数会以 `ARGUMENTS: <value>` 的形式追加。

带命名参数的示例：

```yaml
---
name: deploy
arguments:
  - stackName
  - region
---

部署资源栈 **$stackName** 到地域 **$region**。
```

调用方式：`/deploy my-stack cn-hangzhou`

## 内置变量

| 变量 | 说明 |
|------|------|
| `${SKILL_DIR}` | 技能源文件所在目录的绝对路径 |
| `${SESSION_ID}` | 当前会话标识符 |

## 路径自动激活

带有 `paths` 字段的技能，在模型访问匹配任一 glob 模式的文件时会自动激活：

```yaml
---
name: ros-helper
paths:
  - "*.yml"
  - "templates/**/*.json"
---
```

当匹配文件被访问后，该技能在会话剩余时间内对模型可用。

## 示例

一个生成部署检查清单的简单技能：

```markdown
---
name: checklist
description: 生成部署前检查清单
when_to_use: 当用户想在部署前进行检查时
user_invocable: true
---

审查当前项目并生成部署前检查清单，涵盖：

1. 模板校验状态
2. 参数完整性
3. 安全组规则
4. 资源命名规范
5. 成本估算

如果提供了资源栈名称，还需检查当前资源栈状态。
```

将此文件保存为 `~/.iac-code/skills/checklist.md` 或项目中的 `.iac-code/skills/checklist.md`，然后在 REPL 中通过 `/checklist` 调用 —— 也可以使用 `$checklist`，效果完全相同，但 `$` 触发器只会筛选 skill 候选项。

## 权限

- **内置技能**始终自动允许。
- 无 Shell 命令且无 `allowed_tools` 的**用户/项目技能**自动允许。
- **其他技能**首次使用时需要用户确认。
