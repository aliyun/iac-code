---
title: Skills
description: Create and use reusable prompt templates as slash commands.
---

# Skills

Skills are reusable prompt templates that extend IaC Code with custom slash commands. They let you package complex instructions, tool configurations, and workflow patterns into named commands that can be invoked during a conversation.

## Skill File Formats

Skills are defined as Markdown files with YAML frontmatter. Two formats are supported:

### Single File

A standalone Markdown file named after the skill:

```text
skills/
  deploy-check.md
  code-review.md
```

### Directory

A directory containing a `SKILL.md` file, useful when the skill needs additional reference files:

```text
skills/
  my-skill/
    SKILL.md
    references/
      template.yml
```

## Discovery and Priority

IaC Code discovers skills from multiple locations. When skills share the same name, later sources override earlier ones:

| Priority | Location | Description |
|----------|----------|-------------|
| 1 (lowest) | Bundled | Built-in skills shipped with IaC Code |
| 2 | `~/.iac-code/skills/` | User-global skills (follows `IAC_CODE_CONFIG_DIR`) |
| 3 | `skills/` | Project-level skills directory |
| 4 (highest) | `.iac-code/skills/` | Project config-level skills directory |

Project skill directories are searched upward from the current working directory to the filesystem root.

## Frontmatter Reference

Every skill file starts with YAML frontmatter between `---` delimiters:

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

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | **Yes** | filename stem | Skill name used for invocation. Falls back to the filename if omitted |
| `description` | **Yes** | `""` | One-line description shown in command listings |
| `descriptions` | No | `{}` | Localized descriptions keyed by language code (e.g., `zh-Hans`) |
| `when_to_use` | No | `""` | Hint for the model on when to invoke this skill automatically |
| `argument_hint` | No | `""` | Placeholder shown after the command name |
| `arguments` | No | `[]` | Named argument list for positional substitution |
| `allowed_tools` | No | `[]` | Tools the skill is allowed to use (applies to fork mode) |
| `user_invocable` | No | `true` | Whether the user can invoke this skill directly via `/name` |
| `model` | No | `"inherit"` | Model override for this skill execution |
| `effort` | No | `""` | Thinking effort override |
| `context` | No | `"inline"` | Execution mode: `inline` or `fork` |
| `agent` | No | `"general-purpose"` | Agent type for fork mode |
| `paths` | No | `[]` | Glob patterns for path-based auto-activation |

## Execution Modes

### Inline (default)

The skill's rendered content is injected directly into the current conversation context. The model sees it as additional instructions and acts on them within the same session.

```yaml
context: inline
```

### Fork

The skill runs in an isolated sub-agent with its own context. The sub-agent's final response is returned as a tool result. Use this for self-contained tasks that shouldn't pollute the main conversation.

```yaml
context: fork
agent: general-purpose
```

## Argument Substitution

Skill content can reference arguments passed by the user:

| Placeholder | Description |
|-------------|-------------|
| `$ARGUMENTS` | The full argument string |
| `$0`, `$1`, ... | Positional arguments (space-separated, respects quotes) |
| `$ARGUMENTS[0]`, `$ARGUMENTS[1]` | Explicit indexed access |
| `$argName` | Named argument (matched by position in the `arguments` list) |

If no placeholder is found in the content, arguments are appended as `ARGUMENTS: <value>`.

Example with named arguments:

```yaml
---
name: deploy
arguments:
  - stackName
  - region
---

Deploy the stack **$stackName** in region **$region**.
```

Invocation: `/deploy my-stack cn-hangzhou`

## Built-in Variables

| Variable | Description |
|----------|-------------|
| `${SKILL_DIR}` | Absolute path to the skill's source directory |
| `${SESSION_ID}` | Current session identifier |

## Path-based Auto-activation

Skills with a `paths` field are automatically activated when the model accesses a file matching any of the listed glob patterns:

```yaml
---
name: ros-helper
paths:
  - "*.yml"
  - "templates/**/*.json"
---
```

When a matching file is accessed, the skill becomes available to the model for the remainder of the session.

## Example

A simple skill that generates a deployment checklist:

```markdown
---
name: checklist
description: Generate a pre-deployment checklist
when_to_use: When the user wants to review before deploying
user_invocable: true
---

Review the current project and generate a pre-deployment checklist covering:

1. Template validation status
2. Parameter completeness
3. Security group rules
4. Resource naming conventions
5. Cost estimation

If a stack name is provided, also check the current stack status.
```

Save this as `~/.iac-code/skills/checklist.md` or `.iac-code/skills/checklist.md` in your project. Then invoke it with `/checklist` in the REPL.

## Permissions

- **Bundled skills** are always allowed automatically.
- **User/project skills** with no shell commands and no `allowed_tools` are auto-allowed.
- **Other skills** prompt for user confirmation on first use.
