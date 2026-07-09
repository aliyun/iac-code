---
title: Skills
description: Create and use reusable prompt templates as slash commands.
---

# Skills

Skills are reusable prompt templates that extend IaC Code with custom slash commands. They let you package complex instructions, tool configurations, and workflow patterns into named commands that can be invoked during a conversation.

## Skill-Dateiformat

Ein Skill ist ein Verzeichnis, das eine `SKILL.md`-Datei enthält. Der Verzeichnisname ist der Standard-Skill-Name. Das Verzeichnis kann zusätzliche Referenzdateien enthalten:

```text
skills/
  deploy-check/
    SKILL.md
  my-skill/
    SKILL.md
    references/
      template.yml
```

## Discovery and Priority

IaC Code entdeckt Skills aus mehreren Quellen. Wenn Skills denselben Namen haben, überschreiben Quellen mit höherer Priorität die mit niedrigerer Priorität:

| Priorität | Quelle | Beschreibung |
|----------|----------|-------------|
| 1 (niedrigste) | `~/.iac-code/skills/` | Benutzerweite Skills (folgt `IAC_CODE_CONFIG_DIR`) |
| 2 | `skills/` | Skills-Verzeichnis auf Projektebene |
| 3 | `.iac-code/skills/` | Skills-Verzeichnis auf Projektkonfigurationsebene |
| 4 (höchste) | Gebündelt | Eingebaute Skills, die mit IaC Code ausgeliefert werden. Können nicht durch gleichnamige Benutzer- oder Projekt-Skills überschattet werden |

Projekt-Skill-Verzeichnisse werden von der Wurzel des Git-Repositorys abwärts bis zum aktuellen Arbeitsverzeichnis durchsucht. Wenn Sie sich nicht in einem Git-Repository befinden, wird nur das aktuelle Verzeichnis durchsucht.

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
| `allowed_tools` | No | `[]` | Tools the skill is allowed to use (applies to both inline and fork modes) |
| `user_invocable` | No | `true` | Whether the user can invoke this skill directly via `/name` (or `$name`) |
| `model` | No | `"inherit"` | Model override for this skill execution |
| `effort` | No | `""` | Thinking effort override |
| `context` | No | `"inline"` | Execution mode: `inline` or `fork` |
| `agent` | No | `"general-purpose"` | Agent type for fork mode |
| `paths` | No | `[]` | Glob patterns for path-based auto-activation |

## Skills verwalten

Führen Sie `/skills` im interaktiven REPL aus, um die Skill-Verwaltungsauswahl zu öffnen. Die Auswahl zeigt gefundene gebündelte, Benutzer- und Projekt-Skills mit Quelle, Größe und Aktivierungsstatus. Sie können nach Name oder Beschreibung suchen, nach Name/Quelle/Größe sortieren und Benutzer- oder Projekt-Skills ein- und ausschalten.

Deaktivierte Skills werden in `settings.yml` unter `disabled_skills` gespeichert. Gebündelte Skills sind fest aktiviert und werden nicht in die Deaktivierungsliste geschrieben.

Verwenden Sie `$<skill-name>`, wenn Autovervollständigung und Aufruf nur auf Skills zielen sollen. Das ist nützlich, wenn ein Skill-Name sich mit normalem Text überschneidet oder wenn Sie eingebaute Slash-Befehle vermeiden möchten.

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

Save this as `~/.iac-code/skills/checklist/SKILL.md` or `.iac-code/skills/checklist/SKILL.md` in your project. Then invoke it with `/checklist` in the REPL — or with `$checklist`, which is identical but filters autocomplete suggestions to skills only.

## Permissions

- **Bundled skills** are always allowed automatically.
- **User/project skills** with no shell commands and no `allowed_tools` are auto-allowed.
- **Other skills** prompt for user confirmation on first use.
- **Deaktivierte Benutzer-/Projekt-Skills** werden aus modell-sichtbaren Skill-Listen und automatischen Triggern ausgeblendet; direkte `skill`-Tool-Aufrufe geben einen Fehler für deaktivierte Skills zurück.
